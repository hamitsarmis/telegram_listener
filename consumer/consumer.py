import asyncio
import json
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
HISTORY_LIMIT = int(os.environ.get("HISTORY_LIMIT", "10"))

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
    notes: Optional[str] = None


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _history_key(chat_id, topic_id) -> str:
    return f"tg:history:{chat_id}:{topic_id or 0}"


async def get_history(chat_id, topic_id) -> list[dict]:
    """Returns oldest → newest list of {text, action} entries."""
    raw = await redis_client.lrange(_history_key(chat_id, topic_id), 0, HISTORY_LIMIT - 1)
    return [json.loads(r) for r in reversed(raw)]


async def append_history(chat_id, topic_id, text: str, action: TradeAction) -> None:
    key = _history_key(chat_id, topic_id)
    entry = json.dumps({"text": text, "action": action.model_dump(exclude_none=True)})
    pipe = redis_client.pipeline()
    pipe.lpush(key, entry)
    pipe.ltrim(key, 0, HISTORY_LIMIT - 1)
    await pipe.execute()


async def interpret(text: str, chat_id, topic_id) -> TradeAction | None:
    history = await get_history(chat_id, topic_id)
    messages = []
    for entry in history:
        messages.append({"role": "user", "content": entry["text"]})
        messages.append({"role": "assistant", "content": json.dumps(entry["action"])})
    messages.append({"role": "user", "content": text})

    try:
        resp = await anthropic_client.messages.parse(
            model=ANTHROPIC_MODEL,
            max_tokens=1024,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=messages,
            output_format=TradeAction,
        )
        return resp.parsed_output
    except anthropic.APIError as e:
        print(f"[{_ts()}] !! anthropic error: {e}")
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
        if not text:
            return
        verb = "NEW " if etype == "new" else "EDIT"
        print(f"[{_ts()}] {verb}  [{chat}] {who}: {text}")
        action = await interpret(text, chat_id, topic_id)
        if action:
            print(f"[{_ts()}]    -> {action.model_dump_json(exclude_none=True)}")
            await append_history(chat_id, topic_id, text, action)
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
    print(f"Redis: {REDIS_URL} (history limit={HISTORY_LIMIT})")
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
