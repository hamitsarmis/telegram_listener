# telegram-listener

Pipe Telegram trading-signal channels through Claude into a real broker.

A Telegram user-account listens to a forum topic, every message is interpreted by Claude into a structured trading command (`open_position`, `close_position`, `multi_open`, ...), and a MetaApi-backed executor sends those commands to the broker. Opens default to **pending limit orders** at the price stated in the signal (with stop fallback if the level is on the wrong side of current price); signals that don't carry a price open at market. Kafka is the spine; Redis carries short-term conversation memory so context-dependent messages ("ekleme yapın", "kapatın") resolve correctly.

## Architecture

```
   Telegram                                   broker (MT4/MT5 via MetaApi)
       ▲                                                      ▲
       │  user account (Telethon)                             │ market orders
       │                                                      │
  ┌────┴─────┐    telegram-events    ┌──────────┐  trade-actions  ┌──────────┐
  │ listener │ ─────────────────────▶│ consumer │ ───────────────▶│ executor │
  └──────────┘        Kafka          └─────┬────┘     Kafka       └──────────┘
                                           │
                                           │ history
                                           ▼
                                       ┌────────┐
                                       │ Redis  │
                                       └────────┘
```

Four services in `docker-compose.yml`: `kafka` (KRaft single-node, no host port), `redis` (AOF persistence, no host port), plus the three apps below.

## Components

**`listener/`** — Telethon client logged in as a user. Filters one supergroup + topic, downloads images, caches recent messages, and publishes one JSON event per `new` / `edit` / `delete` to `telegram-events`. Session file and downloaded media live on the host under `./data/`.

**`consumer/`** — pulls `telegram-events`, sends each text/image message to Claude with the trading-prompt system prompt and a strict JSON schema, so the response is guaranteed to be a valid `TradeAction`. **Each `(chat, topic)` pair is a continuous conversation thread**: the full message history is persisted in Redis and replayed on every call, with Anthropic's server-side compaction (`compact-2026-01-12`) summarizing earlier turns when the context grows large. Successful interpretations (excluding `no_action`) are republished to `trade-actions`.

**`executor/`** — wraps `ForexManager` (MetaApi cloud SDK) and consumes `trade-actions`, mapping each command to broker calls: `open_position` → pending limit order at the signal price (or market if no price was given), `close_position` → close-by-symbol or close-all, `cancel_orders` → delete pending orders, SL/TP commands → modify all positions on the symbol. Long-form features like trailing stops, breakeven, and reconnect handling come from `ForexManager` itself.

## Quick start

```bash
# 1. Configure
cp .env.example .env
# Fill in: TG_API_ID, TG_API_HASH, TG_CHAT_ID, TG_TOPIC_ID,
#          ANTHROPIC_API_KEY, METAAPI_TOKEN, METAAPI_ACCOUNT_ID

# 2. Authenticate Telegram (once, interactive — saves data/listener.session)
docker compose run --rm -it listener python login.py

# 3. Run everything
docker compose up --build
```

Tail just the parsed actions:

```bash
docker compose logs -f consumer | grep ' -> '
```

Or inspect the Kafka topic directly:

```bash
docker exec -it kafka kafka-console-consumer.sh \
  --bootstrap-server localhost:9092 --topic trade-actions --from-beginning
```

## Layout

```
.
├── .env                  # secrets + per-deploy config (gitignored)
├── docker-compose.yml    # kafka + redis + listener + consumer + executor
├── data/                 # listener.session + media/ — runtime state, gitignored
├── listener/             # Telethon → telegram-events
├── consumer/             # Claude + Redis history → trade-actions
└── executor/             # ForexManager → MetaApi → broker
```

## Configuration reference

| Var | Purpose |
|---|---|
| `TG_API_ID`, `TG_API_HASH` | Telegram app credentials (https://my.telegram.org/apps) |
| `TG_CHAT_ID`, `TG_TOPIC_ID` | Supergroup dialog ID (`-100…`) and forum topic to listen on |
| `ANTHROPIC_API_KEY` | Claude API key |
| `ANTHROPIC_MODEL` | Default `claude-opus-4-7`. `claude-sonnet-4-6` works too and gets prompt caching for free. |
| (no history limit) | Each `(chat, topic)` thread is unbounded — Anthropic's server-side compaction (`compact-2026-01-12` beta) summarizes earlier turns when context approaches its limit. |
| `METAAPI_TOKEN`, `METAAPI_ACCOUNT_ID` | Broker bridge (https://app.metaapi.cloud/) |
| `DEFAULT_VOLUME` | Lot size for every market order (default `0.01`) |
| `KAFKA_TOPIC`, `KAFKA_ACTIONS_TOPIC` | Topic names if you want to override |

## Action coverage

| Command | Broker action |
|---|---|
| `open_position`, `add_position` | pending limit order at `entry_price` (limit/stop chosen by current price). Falls back to a market order at `DEFAULT_VOLUME` if no `entry_price` was given. |
| `multi_open` | one pending order per price in `entries` |
| `close_position` (symbol set) | close all positions for that symbol |
| `close_position` (symbol null) | close every position |
| `close_if_profit` | close only positions with `profit > 0` |
| `partial_close` | currently treated as a full close (partial sizing TBD) |
| `cancel_orders`, `cancel_all_orders` | delete all pending orders |
| `update_stoploss` | modify SL on every open position for the symbol |
| `update_takeprofit` | modify TP on every open position for the symbol |
| `update_takeprofit_zone` | midpoint of `tp_zone` becomes the new TP |
| `update_takeprofit_from_pips` | TP set per position direction: buy → `base + pips × PIP_VALUE`, sell → `base − pips × PIP_VALUE` |
| `move_stop_to_entry` | each position's SL moves to its own openPrice (per-position breakeven) |
| `no_action` | dropped at the consumer — never reaches the executor |

## Caveats

- `DEFAULT_VOLUME` is a single constant — no per-signal sizing yet, and `partial_close` currently closes the whole position.
- `PIP_VALUE` is a single constant (`0.01`) used for `update_takeprofit_from_pips`. Broker-specific — change it in `executor.py` if your broker quotes pips differently.
- All position-modification actions (`update_stoploss`, `update_takeprofit*`, `move_stop_to_entry`) act on every open position on the symbol. No per-position targeting.
- The Redis history cache is in-memory persistence (AOF) — fine on one box, replace with managed Redis for production.
- The executor is set to `auto_offset_reset=latest`: actions published while it's offline are skipped, not replayed. Restarting the executor never re-fires old trades.
- Kafka and Redis bind only to the compose network — nothing is exposed to the host. To run a service on the host instead of in the container, expose the port temporarily or use `docker exec`.

## License

Use it, fork it, change it, ship it. No warranty, no restrictions, no attribution required. Do whatever you want with it.

If your country needs a formal license to make the above stick: this project is released under the MIT License.
