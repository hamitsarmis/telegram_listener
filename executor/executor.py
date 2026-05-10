import asyncio
import json
import os
from datetime import datetime

from aiokafka import AIOKafkaConsumer
from dotenv import find_dotenv, load_dotenv

from forex_manager import ForexManager

load_dotenv(find_dotenv(usecwd=True))

KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "kafka:9092")
KAFKA_ACTIONS_TOPIC = os.environ.get("KAFKA_ACTIONS_TOPIC", "trade-actions")
GROUP_ID = os.environ.get("EXECUTOR_GROUP_ID", "forex-executor")

METAAPI_TOKEN = os.environ["METAAPI_TOKEN"]
METAAPI_ACCOUNT_ID = os.environ["METAAPI_ACCOUNT_ID"]
DEFAULT_VOLUME = float(os.environ.get("DEFAULT_VOLUME", "0.01"))


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _fx_side(side: str | None) -> str | None:
    return {"long": "buy", "short": "sell"}.get(side or "")


async def execute(manager: ForexManager, action: dict) -> None:
    a = action.get("action")
    sym = action.get("symbol")
    side = _fx_side(action.get("side"))

    if a in ("open_position", "add_position"):
        if not sym or not side:
            print(f"[{_ts()}] skip {a}: missing symbol/side ({action})")
            return
        await manager.open_market_order(symbol=sym, side=side, volume=DEFAULT_VOLUME)

    elif a == "multi_open":
        if not sym or not side:
            print(f"[{_ts()}] skip multi_open: missing symbol/side")
            return
        entries = action.get("entries") or []
        for _ in entries:
            await manager.open_market_order(symbol=sym, side=side, volume=DEFAULT_VOLUME)

    elif a == "close_position":
        if sym:
            await manager.close_positions_for_symbol(sym)
        else:
            await manager.close_all_positions()

    elif a == "close_if_profit":
        positions = await manager.get_positions()
        for pos in positions:
            if sym and pos.get("symbol") != sym:
                continue
            if (pos.get("profit") or 0) > 0:
                await manager.close_position_market(pos["id"])

    elif a in ("cancel_orders", "cancel_all_orders"):
        await manager.delete_all_pending_orders()

    elif a == "no_action":
        return

    else:
        print(f"[{_ts()}] action not implemented yet: {a}")


async def main() -> None:
    manager = ForexManager(token=METAAPI_TOKEN, account_id=METAAPI_ACCOUNT_ID)
    await manager.start()
    if not await manager.wait_ready(timeout=60):
        raise RuntimeError("MetaApi connection not ready within 60s")
    print(f"MetaApi ready (account {METAAPI_ACCOUNT_ID})")

    consumer = AIOKafkaConsumer(
        KAFKA_ACTIONS_TOPIC,
        bootstrap_servers=KAFKA_BOOTSTRAP,
        group_id=GROUP_ID,
        # latest: don't replay old actions on startup — could double-execute trades
        auto_offset_reset="latest",
        enable_auto_commit=True,
        value_deserializer=lambda v: json.loads(v.decode()),
    )
    await consumer.start()
    print(f"Consuming {KAFKA_ACTIONS_TOPIC!r} from {KAFKA_BOOTSTRAP} (group={GROUP_ID})")

    try:
        async for msg in consumer:
            envelope = msg.value
            action = envelope.get("action") or {}
            print(f"[{_ts()}] EXEC {envelope.get('source_text')!r} -> {action}")
            try:
                await execute(manager, action)
            except Exception as e:
                print(f"[{_ts()}] !! execute error: {e}")
    finally:
        await consumer.stop()
        await manager.stop()


if __name__ == "__main__":
    asyncio.run(main())
