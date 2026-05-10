# telegram-listener

Pipe Telegram trading-signal channels through Claude into a real broker.

A Telegram user-account listens to a forum topic, every message is interpreted by Claude into a structured trading command (`open_position`, `close_position`, `multi_open`, ...), and a MetaApi-backed executor turns those commands into market orders. Kafka is the spine; Redis carries short-term conversation memory so context-dependent messages ("ekleme yapın", "kapatın") resolve correctly.

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

**`consumer/`** — pulls `telegram-events`, sends each text message to Claude with the trading-prompt system prompt and a Pydantic-validated JSON schema (`messages.parse()`), so the response is guaranteed to be a valid `TradeAction`. Recent (text → action) pairs per `(chat, topic)` are kept in Redis as a capped list and replayed as alternating user/assistant turns, giving the model the context it needs to resolve "add to position" or "close it" against the right open trade. Successful interpretations (excluding `no_action`) are republished to `trade-actions`.

**`executor/`** — wraps `ForexManager` (MetaApi cloud SDK) and consumes `trade-actions`, mapping each command to broker calls: `open_position` → market order, `close_position` → close-by-symbol or close-all, `cancel_orders` → delete pending orders, etc. Long-form features like trailing stops, breakeven, and reconnect handling come from `ForexManager` itself.

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
| `HISTORY_LIMIT` | Per-chat conversation turns kept in Redis (default 10) |
| `METAAPI_TOKEN`, `METAAPI_ACCOUNT_ID` | Broker bridge (https://app.metaapi.cloud/) |
| `DEFAULT_VOLUME` | Lot size for every market order (default `0.01`) |
| `KAFKA_TOPIC`, `KAFKA_ACTIONS_TOPIC` | Topic names if you want to override |

## Action coverage

| Command | Broker action |
|---|---|
| `open_position`, `add_position` | market order at `DEFAULT_VOLUME` |
| `multi_open` | one market order per entry |
| `close_position` (symbol set) | close all positions for that symbol |
| `close_position` (symbol null) | close every position |
| `close_if_profit` | close only positions with `profit > 0` |
| `cancel_orders`, `cancel_all_orders` | delete pending orders |
| `update_stoploss`, `update_takeprofit`, `move_stop_to_entry`, `partial_close`, `update_takeprofit_zone`, `update_takeprofit_from_pips` | logged, not yet wired |
| `no_action` | dropped at the consumer — never reaches the executor |

## Caveats

- `DEFAULT_VOLUME` is a single constant — no per-signal sizing yet.
- `multi_open` opens market orders, not pending limit orders at the specified entries.
- The Redis history cache is in-memory persistence (AOF) — fine on one box, replace with managed Redis for production.
- The executor is set to `auto_offset_reset=latest`: actions published while it's offline are skipped, not replayed. Restarting the executor never re-fires old trades.
- Kafka and Redis bind only to the compose network — nothing is exposed to the host. To run a service on the host instead of in the container, expose the port temporarily or use `docker exec`.

## License

Use it, fork it, change it, ship it. No warranty, no restrictions, no attribution required. Do whatever you want with it.

If your country needs a formal license to make the above stick: this project is released under the MIT License.
