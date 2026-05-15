import asyncio
import base64
import json
import mimetypes
import os
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

import anthropic
import openai
import redis.asyncio as redis
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from anthropic import AsyncAnthropic
from dotenv import find_dotenv, load_dotenv
from openai import AsyncOpenAI
from pydantic import BaseModel

load_dotenv(find_dotenv(usecwd=True))

KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "kafka:9092")
KAFKA_TOPIC = os.environ.get("KAFKA_TOPIC", "telegram-events")
KAFKA_ACTIONS_TOPIC = os.environ.get("KAFKA_ACTIONS_TOPIC", "trade-actions")
GROUP_ID = os.environ.get("KAFKA_GROUP_ID", "telegram-consumer")

USE_DEEPSEEK = os.environ.get("USE_DEEPSEEK", "false").strip().lower() in ("true", "1", "yes", "on")
BACKEND = "deepseek" if USE_DEEPSEEK else "anthropic"

ANTHROPIC_MODEL_TEXT = os.environ.get("ANTHROPIC_MODEL_TEXT", "claude-haiku-4-5")
ANTHROPIC_MODEL_IMAGE = os.environ.get("ANTHROPIC_MODEL_IMAGE", "claude-opus-4-7")

DEEPSEEK_MODEL_TEXT = os.environ.get("DEEPSEEK_MODEL_TEXT", "deepseek-chat")
DEEPSEEK_MODEL_IMAGE = os.environ.get("DEEPSEEK_MODEL_IMAGE", "deepseek-chat")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
# DeepSeek has no server-side compaction (Anthropic does via compact_20260112);
# without a cap the thread grows until it exceeds the model's context window and
# every subsequent call for that chat fails permanently.
DEEPSEEK_MAX_TURNS = int(os.environ.get("DEEPSEEK_MAX_TURNS", "20"))

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")

SYSTEM_PROMPT = (Path(__file__).parent / "prompt.md").read_text()

# One backend per process. The unused client is left as None so attribute access fails
# loudly if the dispatch ever picks the wrong path.
anthropic_client: AsyncAnthropic | None = None
deepseek_client: AsyncOpenAI | None = None
if USE_DEEPSEEK:
    # Validate explicitly: passing api_key=None to AsyncOpenAI would silently
    # fall back to OPENAI_API_KEY in the env, sending an OpenAI bearer token
    # to api.deepseek.com.
    _deepseek_key = os.environ.get("DEEPSEEK_API_KEY")
    if not _deepseek_key:
        raise RuntimeError("DEEPSEEK_API_KEY is required when USE_DEEPSEEK=true")
    deepseek_client = AsyncOpenAI(api_key=_deepseek_key, base_url=DEEPSEEK_BASE_URL)
else:
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
    # Namespaced by backend: Anthropic content blocks and OpenAI message format
    # are not interchangeable, so flipping USE_DEEPSEEK must not reuse the same thread.
    return f"tg:thread:{BACKEND}:{chat_id}:{topic_id or 0}"


async def get_thread(chat_id, topic_id) -> list[dict]:
    key = _thread_key(chat_id, topic_id)
    raw = await redis_client.get(key)
    if raw is None and not USE_DEEPSEEK:
        # Pre-namespace deployments wrote `tg:thread:{chat_id}:{topic_id}`. Migrate
        # on first read so existing chats keep their captured S/R history.
        legacy = f"tg:thread:{chat_id}:{topic_id or 0}"
        raw = await redis_client.get(legacy)
        if raw is not None:
            await redis_client.rename(legacy, key)
    return json.loads(raw) if raw else []


async def save_thread(chat_id, topic_id, messages: list[dict]) -> None:
    await redis_client.set(_thread_key(chat_id, topic_id), json.dumps(messages))


def _load_image(media_path: str) -> tuple[str, str] | None:
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
    return mime, data


def _image_block_anthropic(media_path: str) -> dict | None:
    loaded = _load_image(media_path)
    if loaded is None:
        return None
    mime, data = loaded
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": mime, "data": data},
    }


def _image_block_openai(media_path: str) -> dict | None:
    loaded = _load_image(media_path)
    if loaded is None:
        return None
    mime, data = loaded
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{data}"},
    }


async def interpret(
    text: str, chat_id, topic_id, media_path: str | None = None
) -> TradeAction | None:
    if USE_DEEPSEEK:
        return await _interpret_deepseek(text, chat_id, topic_id, media_path)
    return await _interpret_anthropic(text, chat_id, topic_id, media_path)


async def _interpret_anthropic(
    text: str, chat_id, topic_id, media_path: str | None = None
) -> TradeAction | None:
    messages = await get_thread(chat_id, topic_id)

    # Build the new user turn — image content (if any) plus text.
    user_content: list[dict] = []
    has_image = False
    if media_path:
        block = _image_block_anthropic(media_path)
        if block is not None:
            user_content.append(block)
            has_image = True
    if text:
        user_content.append({"type": "text", "text": text})
    elif user_content:
        user_content.append({"type": "text", "text": "(image only — interpret as trading signal)"})

    if not user_content:
        return None

    messages.append({"role": "user", "content": user_content})

    # Vision-capable Opus only when the turn actually carries an image — text-only
    # turns go to Haiku (~15× cheaper). Mid-thread model switches are fine: the
    # message history is portable across Anthropic models.
    model = ANTHROPIC_MODEL_IMAGE if has_image else ANTHROPIC_MODEL_TEXT

    try:
        resp = await anthropic_client.beta.messages.create(
            betas=["compact-2026-01-12"],
            model=model,
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


def _strip_image_blocks(messages: list[dict]) -> list[dict]:
    """Drop image_url blocks from prior turns.

    Two reasons: (1) the text-only DeepSeek model rejects historical image_url
    content, and (2) base64 image bytes inflate the request by 70-90K tokens per
    image and re-paying that on every later turn quickly blows the context
    window. The assistant's structured JSON reply already captured what was in
    the chart (S/R levels, etc.), so the bytes themselves are dead weight.
    """
    out: list[dict] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            text_parts = [b for b in content if b.get("type") == "text"]
            if text_parts:
                joined = "\n".join(b["text"] for b in text_parts)
                out.append({**m, "content": joined})
            else:
                out.append({**m, "content": "(image attachment omitted from history)"})
        else:
            out.append(m)
    return out


async def _interpret_deepseek(
    text: str, chat_id, topic_id, media_path: str | None = None
) -> TradeAction | None:
    messages = await get_thread(chat_id, topic_id)

    user_content: list[dict] = []
    has_image = False
    if media_path:
        block = _image_block_openai(media_path)
        if block is not None:
            user_content.append(block)
            has_image = True
    if text:
        user_content.append({"type": "text", "text": text})
    elif user_content:
        user_content.append({"type": "text", "text": "(image only — interpret as trading signal)"})

    if not user_content:
        return None

    # OpenAI-style: text-only turns can use a plain string for content; multimodal
    # turns must be the list form. Both shapes are accepted by the chat API.
    if has_image:
        new_msg_content: str | list[dict] = user_content
    else:
        new_msg_content = "\n".join(b["text"] for b in user_content if b.get("type") == "text")
    messages.append({"role": "user", "content": new_msg_content})

    model = DEEPSEEK_MODEL_IMAGE if has_image else DEEPSEEK_MODEL_TEXT

    # Bound the request: strip historical image bytes (always — see helper docstring)
    # and keep only the most recent N user/assistant pairs.
    history_for_request = _strip_image_blocks(messages)
    if len(history_for_request) > DEEPSEEK_MAX_TURNS * 2:
        history_for_request = history_for_request[-DEEPSEEK_MAX_TURNS * 2:]
    full_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history_for_request

    try:
        resp = await deepseek_client.chat.completions.create(
            model=model,
            max_tokens=1024,
            messages=full_messages,
            response_format={"type": "json_object"},
        )
    except openai.OpenAIError as e:
        print(f"[{_ts()}] !! deepseek error: {e}")
        return None

    raw = resp.choices[0].message.content or ""
    # Persist the bounded, image-stripped form so Redis size stays in check —
    # we already sent that exact view to the model, so nothing the assistant
    # responded to is lost.
    persisted = history_for_request + [{"role": "assistant", "content": raw}]
    await save_thread(chat_id, topic_id, persisted)

    try:
        return TradeAction.model_validate_json(raw)
    except Exception as e:
        print(f"[{_ts()}] !! parse error: {e} | raw={raw!r}")
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
    print(f"Redis: {REDIS_URL} (per-chat thread, namespaced by backend)")
    if USE_DEEPSEEK:
        print(f"Backend: DeepSeek @ {DEEPSEEK_BASE_URL}")
        active_text, active_image = DEEPSEEK_MODEL_TEXT, DEEPSEEK_MODEL_IMAGE
    else:
        print("Backend: Anthropic")
        active_text, active_image = ANTHROPIC_MODEL_TEXT, ANTHROPIC_MODEL_IMAGE
    print(f"Models: text={active_text} image={active_image}")
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
