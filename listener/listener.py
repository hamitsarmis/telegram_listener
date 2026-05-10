import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from aiokafka import AIOKafkaProducer
from dotenv import find_dotenv, load_dotenv
from telethon import TelegramClient, events

load_dotenv(find_dotenv(usecwd=True))

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION = os.environ.get("TG_SESSION", "listener")
CHAT_ID = int(os.environ["TG_CHAT_ID"]) if os.environ.get("TG_CHAT_ID") else None
TOPIC_ID = int(os.environ["TG_TOPIC_ID"]) if os.environ.get("TG_TOPIC_ID") else None

KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "localhost:29092")
KAFKA_TOPIC = os.environ.get("KAFKA_TOPIC", "telegram-events")

CHATS_FILTER = [CHAT_ID] if CHAT_ID is not None else None

MEDIA_DIR = Path("media")
MEDIA_DIR.mkdir(exist_ok=True)

# Cache of recent messages so edits/deletes can include the prior content.
# Telegram's delete event only carries message IDs, so without a cache we have nothing to publish.
MESSAGE_CACHE: dict[tuple[int, int], dict] = {}
CACHE_LIMIT = 5000

producer: AIOKafkaProducer | None = None


def _in_topic(msg) -> bool:
    if TOPIC_ID is None:
        return True
    rt = getattr(msg, "reply_to", None)
    if rt is None:
        return False
    return (getattr(rt, "reply_to_top_id", None) or getattr(rt, "reply_to_msg_id", None)) == TOPIC_ID


def _cache_put(chat_id: int, msg_id: int, data: dict) -> None:
    if len(MESSAGE_CACHE) >= CACHE_LIMIT:
        MESSAGE_CACHE.pop(next(iter(MESSAGE_CACHE)))
    MESSAGE_CACHE[(chat_id, msg_id)] = data


def _sender_dict(sender) -> dict | None:
    if sender is None:
        return None
    name = (getattr(sender, "first_name", None) or getattr(sender, "title", None) or "").strip()
    return {
        "id": getattr(sender, "id", None),
        "name": name or None,
        "username": getattr(sender, "username", None),
    }


def _fmt_sender(d: dict | None) -> str:
    if not d:
        return "?"
    if d.get("username"):
        return f"{d.get('name') or ''} (@{d['username']})".strip()
    return d.get("name") or str(d.get("id") or "?")


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


async def publish(event: dict) -> None:
    if producer is None:
        return
    payload = json.dumps(event, default=str).encode()
    key = str(event.get("chat_id") or "").encode() or None
    await producer.send_and_wait(KAFKA_TOPIC, payload, key=key)


client = TelegramClient(SESSION, API_ID, API_HASH)


@client.on(events.NewMessage(chats=CHATS_FILTER))
async def on_new_message(event: events.NewMessage.Event) -> None:
    msg = event.message
    if not _in_topic(msg):
        return
    sender = _sender_dict(await event.get_sender())
    chat = await event.get_chat()
    chat_title = getattr(chat, "title", None) or _fmt_sender(_sender_dict(chat))

    text = msg.message or ""
    media_path: str | None = None

    if msg.photo or (msg.document and (msg.document.mime_type or "").startswith("image/")):
        fname = MEDIA_DIR / f"{event.chat_id}_{msg.id}"
        media_path = await msg.download_media(file=str(fname))
        print(f"[{_ts()}] NEW   [{chat_title}] {_fmt_sender(sender)}: <image> {media_path}"
              + (f" caption={text!r}" if text else ""))
    else:
        print(f"[{_ts()}] NEW   [{chat_title}] {_fmt_sender(sender)}: {text}")

    record = {
        "type": "new",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "chat_id": event.chat_id,
        "chat_title": chat_title,
        "message_id": msg.id,
        "topic_id": TOPIC_ID,
        "sender": sender,
        "text": text,
        "media": media_path,
    }
    _cache_put(event.chat_id, msg.id, record)
    await publish(record)


@client.on(events.MessageEdited(chats=CHATS_FILTER))
async def on_edit(event: events.MessageEdited.Event) -> None:
    if not _in_topic(event.message):
        return
    key = (event.chat_id, event.message.id)
    prev = MESSAGE_CACHE.get(key)
    new_text = event.message.message or ""
    sender = _sender_dict(await event.get_sender())
    chat = await event.get_chat()
    chat_title = getattr(chat, "title", None) or _fmt_sender(_sender_dict(chat))
    old_text = prev["text"] if prev else None

    print(f"[{_ts()}] EDIT  [{chat_title}] {_fmt_sender(sender)}: {old_text!r} -> {new_text!r}")

    if prev:
        prev["text"] = new_text

    await publish({
        "type": "edit",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "chat_id": event.chat_id,
        "chat_title": chat_title,
        "message_id": event.message.id,
        "topic_id": TOPIC_ID,
        "sender": sender,
        "old_text": old_text,
        "new_text": new_text,
    })


@client.on(events.MessageDeleted(chats=CHATS_FILTER))
async def on_delete(event: events.MessageDeleted.Event) -> None:
    # Topic filtering relies on the cache: we only cached messages from our target topic,
    # so any delete that hits cache is in-topic. Uncached deletes (other topics, pre-startup) are skipped.
    for msg_id in event.deleted_ids:
        key = (event.chat_id, msg_id)
        prev = MESSAGE_CACHE.get(key)
        if not prev:
            continue
        print(f"[{_ts()}] DEL   [{prev['chat_title']}] {_fmt_sender(prev['sender'])}: {prev['text']!r}"
              + (f" media={prev['media']}" if prev["media"] else ""))
        await publish({
            "type": "delete",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "chat_id": event.chat_id,
            "chat_title": prev["chat_title"],
            "message_id": msg_id,
            "topic_id": prev.get("topic_id"),
            "sender": prev["sender"],
            "cached_text": prev["text"],
            "cached_media": prev["media"],
        })
        MESSAGE_CACHE.pop(key, None)


async def main() -> None:
    global producer
    producer = AIOKafkaProducer(bootstrap_servers=KAFKA_BOOTSTRAP)
    await producer.start()
    print(f"Kafka producer connected: {KAFKA_BOOTSTRAP} -> topic {KAFKA_TOPIC!r}")

    try:
        await client.start()
        me = await client.get_me()
        print(f"Listening as {_fmt_sender(_sender_dict(me))}. Ctrl+C to stop.")
        await client.run_until_disconnected()
    finally:
        await producer.stop()


if __name__ == "__main__":
    asyncio.run(main())
