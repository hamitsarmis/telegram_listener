import asyncio
import base64
import json
import mimetypes
import os
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

import anthropic
import redis.asyncio as redis
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from anthropic import AsyncAnthropic
from dotenv import find_dotenv, load_dotenv
from pydantic import BaseModel

load_dotenv(find_dotenv(usecwd=True))

KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "kafka:9092")
KAFKA_TOPIC = os.environ.get("KAFKA_TOPIC", "telegram-events")
KAFKA_ACTIONS_TOPIC = os.environ.get("KAFKA_ACTIONS_TOPIC", "trade-actions")
GROUP_ID = os.environ.get("KAFKA_GROUP_ID", "telegram-consumer")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-7")
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")

SYSTEM_PROMPT = (Path(__file__).parent / "prompt.md").read_text()

anthropic_client = AsyncAnthropic()  # reads ANTHROPIC_API_KEY from env
redis_client = redis.from_url(REDIS_URL, decode_responses=True)
producer: AIOKafkaProducer | None = None


Action = Literal[
    "open_position",
    "add_position",
    "multi_open",
    "close_position",
    "close_if_profit",
    "update_stoploss",
    "update_takeprofit",
    "move_stop_to_entry",
    "partial_close",
    "cancel_orders",
    "cancel_all_orders",
    "update_takeprofit_zone",
    "update_takeprofit_from_pips",
    "no_action",
]


class TradeAction(BaseModel):
    action: Action
    symbol: Optional[str] = None
    side: Optional[Literal["long", "short"]] = None
    entry_price: Optional[float] = None
    entries: Optional[list[float]] = None
    sl_price: Optional[float] = None
    sl_pips: Optional[float] = None
    tp_price: Optional[float] = None
    tp_zone: Optional[list[float]] = None
    close_percent: Optional[float] = None
    base_price: Optional[float] = None
    pips: Optional[float] = None
    support_levels: Optional[list[float]] = None
    resistance_levels: Optional[list[float]] = None
    notes: Optional[str] = None


def _strict_schema(model_class) -> dict:
    """Pydantic → Anthropic JSON Schema.

    Makes every property `required` while keeping `Optional[X]` nullable
    (`anyOf: [X, null]` is left intact). The resulting schema is a fixed
    shape: every field always appears, with a value of either the declared
    type or null. Without this, ~16 optional fields produce a combinatorial
    grammar (any subset, any order) that trips Anthropic's structured-output
    compiler with "Schema is too complex" / "Grammar compilation timed out".

    Also sets `additionalProperties: false` on every object — Anthropic's
    structured-output API rejects schemas without it.
    """
    schema = model_class.model_json_schema()

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"].keys())
                node["additionalProperties"] = False
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    return schema


TRADE_ACTION_SCHEMA = _strict_schema(TradeAction)


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _thread_key(chat_id, topic_id) -> str:
    return f"tg:thread:{chat_id}:{topic_id or 0}"


async def get_thread(chat_id, topic_id) -> list[dict]:
    raw = await redis_client.get(_thread_key(chat_id, topic_id))
    return json.loads(raw) if raw else []


async def save_thread(chat_id, topic_id, messages: list[dict]) -> None:
    await redis_client.set(_thread_key(chat_id, topic_id), json.dumps(messages))


def _image_block(media_path: str) -> dict | None:
    # The path comes from the listener's POV (e.g. "media/-100..._123.jpg").
    # Inside the consumer container, ./data/media is mounted at /app/media,
    # so the same relative path resolves correctly.
    p = Path(media_path)
    if not p.exists() or not p.is_file():
        return None
    mime = mimetypes.guess_type(str(p))[0] or "image/jpeg"
    if not mime.startswith("image/"):
        return None
    data = base64.standard_b64encode(p.read_bytes()).decode()
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": mime, "data": data},
    }


async def interpret(
    text: str, chat_id, topic_id, media_path: str | None = None
) -> TradeAction | None:
    messages = await get_thread(chat_id, topic_id)

    # Build the new user turn — image content (if any) plus text.
    user_content: list[dict] = []
    if media_path:
        block = _image_block(media_path)
        if block is not None:
            user_content.append(block)
    if text:
        user_content.append({"type": "text", "text": text})
    elif user_content:
        user_content.append({"type": "text", "text": "(image only — interpret as trading signal)"})

    if not user_content:
        return None

    messages.append({"role": "user", "content": user_content})

    try:
        resp = await anthropic_client.beta.messages.create(
            betas=["compact-2026-01-12"],
            model=ANTHROPIC_MODEL,
            max_tokens=1024,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=messages,
            context_management={"edits": [{"type": "compact_20260112"}]},
            output_config={
                "format": {"type": "json_schema", "schema": TRADE_ACTION_SCHEMA},
            },
        )
    except anthropic.APIError as e:
        print(f"[{_ts()}] !! anthropic error: {e}")
        # Don't persist the unanswered user turn; otherwise the thread drifts.
        return None

    # Save the FULL response.content back — compaction blocks must be preserved
    # or the next call loses the compacted history state.
    serialized_content = [b.model_dump(exclude_none=True) for b in resp.content]
    messages.append({"role": "assistant", "content": serialized_content})
    await save_thread(chat_id, topic_id, messages)

    # Extract the JSON action from the (single) text block produced by structured output.
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            try:
                return TradeAction.model_validate_json(block.text)
            except Exception as e:
                print(f"[{_ts()}] !! parse error: {e} | raw={block.text!r}")
                return None
    return None


async def handle_event(event: dict) -> None:
    etype = event.get("type")
    chat = event.get("chat_title") or event.get("chat_id")
    chat_id = event.get("chat_id")
    topic_id = event.get("topic_id")
    sender = event.get("sender") or {}
    who = sender.get("username") and f"@{sender['username']}" or sender.get("name") or "?"

    if etype in ("new", "edit"):
        text = (event.get("text") or event.get("new_text") or "").strip()
        media = event.get("media")
        if not text and not media:
            return
        verb = "NEW " if etype == "new" else "EDIT"
        body = text or f"<image {media}>"
        print(f"[{_ts()}] {verb}  [{chat}] {who}: {body}")
        action = await interpret(text, chat_id, topic_id, media_path=media)
        if action:
            print(f"[{_ts()}]    -> {action.model_dump_json(exclude_none=True)}")
            if producer is not None and action.action != "no_action":
                envelope = {
                    "chat_id": chat_id,
                    "topic_id": topic_id,
                    "message_id": event.get("message_id"),
                    "source_text": text,
                    "action": action.model_dump(exclude_none=True),
                }
                await producer.send_and_wait(
                    KAFKA_ACTIONS_TOPIC,
                    json.dumps(envelope).encode(),
                    key=str(chat_id or "").encode() or None,
                )
    elif etype == "delete":
        cached = event.get("cached_text")
        print(f"[{_ts()}] DEL   [{chat}] {who}: {cached!r}")
    else:
        print(f"[{_ts()}] ??    {event}")


async def main() -> None:
    global producer
    consumer = AIOKafkaConsumer(
        KAFKA_TOPIC,
        bootstrap_servers=KAFKA_BOOTSTRAP,
        group_id=GROUP_ID,
        auto_offset_reset="earliest",
        enable_auto_commit=True,
        value_deserializer=lambda v: json.loads(v.decode()),
    )
    producer = AIOKafkaProducer(bootstrap_servers=KAFKA_BOOTSTRAP)
    await consumer.start()
    await producer.start()
    await redis_client.ping()
    print(f"Consuming {KAFKA_TOPIC!r} from {KAFKA_BOOTSTRAP} (group={GROUP_ID})")
    print(f"Publishing actions to {KAFKA_ACTIONS_TOPIC!r}")
    print(f"Redis: {REDIS_URL} (per-chat thread w/ server-side compaction)")
    print(f"Claude model: {ANTHROPIC_MODEL}")
    try:
        async for msg in consumer:
            try:
                await handle_event(msg.value)
            except Exception as e:
                print(f"[{_ts()}] !! handler error: {e}")
    finally:
        await consumer.stop()
        await producer.stop()
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
