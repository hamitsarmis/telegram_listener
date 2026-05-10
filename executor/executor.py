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

# Pip size in price units. Used by update_takeprofit_from_pips. Broker-dependent.
PIP_VALUE = 0.01


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
        entry = action.get("entry_price")
        if entry is None:
            # No entry price → open immediately at market.
            await manager.open_market_order(symbol=sym, side=side, volume=DEFAULT_VOLUME)
        else:
            await manager.open_pending_order(
                symbol=sym, side=side, volume=DEFAULT_VOLUME,
                entry_price=entry,
                sl_price=action.get("sl_price"),
                tp_price=action.get("tp_price"),
            )

    elif a == "multi_open":
        if not sym or not side:
            print(f"[{_ts()}] skip multi_open: missing symbol/side")
            return
        entries = action.get("entries") or []
        if not entries:
            print(f"[{_ts()}] skip multi_open: no entries")
            return
        for entry in entries:
            await manager.open_pending_order(
                symbol=sym, side=side, volume=DEFAULT_VOLUME,
                entry_price=entry,
                sl_price=action.get("sl_price"),
                tp_price=action.get("tp_price"),
            )

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

    elif a == "update_stoploss":
        sl = action.get("sl_price")
        if not sym or sl is None:
            print(f"[{_ts()}] skip update_stoploss: need symbol and sl_price ({action})")
            return
        await manager.modify_positions_for_symbol(sym, stop_loss=sl)

    elif a == "update_takeprofit":
        tp = action.get("tp_price")
        if not sym or tp is None:
            print(f"[{_ts()}] skip update_takeprofit: need symbol and tp_price ({action})")
            return
        await manager.modify_positions_for_symbol(sym, take_profit=tp)

    elif a == "move_stop_to_entry":
        if not sym:
            print(f"[{_ts()}] skip move_stop_to_entry: missing symbol")
            return
        await manager.move_stop_to_entry_for_symbol(sym)

    elif a == "partial_close":
        # Treated as a full close for now — partial sizing comes later.
        if sym:
            await manager.close_positions_for_symbol(sym)
        else:
            await manager.close_all_positions()

    elif a == "update_takeprofit_zone":
        zone = action.get("tp_zone") or []
        if not sym or len(zone) < 2:
            print(f"[{_ts()}] skip update_takeprofit_zone: need symbol and 2-element tp_zone ({action})")
            return
        # Use the midpoint of the zone as the single broker-side TP.
        tp = (zone[0] + zone[1]) / 2
        await manager.modify_positions_for_symbol(sym, take_profit=tp)

    elif a == "update_takeprofit_from_pips":
        base = action.get("base_price")
        pips = action.get("pips")
        if not sym or base is None or pips is None:
            print(f"[{_ts()}] skip update_takeprofit_from_pips: need symbol, base_price, pips ({action})")
            return
        distance = pips * PIP_VALUE
        await manager.update_takeprofit_from_pips_for_symbol(sym, base, distance)

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
