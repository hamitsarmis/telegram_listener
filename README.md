# telegram-listener

Pipe Telegram trading-signal channels through Claude into a real broker.

A Telegram user-account listens to a forum topic, every message is interpreted by Claude into a structured trading command (`open_position`, `close_position`, `multi_open`, ...), and a MetaApi-backed executor sends those commands to the broker. Opens default to **pending limit orders** at the price stated in the signal (with stop fallback if the level is on the wrong side of current price); signals that don't carry a price open at market. Kafka is the spine; Redis carries short-term conversation memory so context-dependent messages ("ekleme yapın", "kapatın") resolve correctly.

## Architecture

```
   Telegram                                   broker (MT4/MT5 via MetaApi)
       ▲                                                      ▲
       │  user account (Telethon)                             │ limit orders
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

**`consumer/`** — pulls `telegram-events`, sends each text/image message to Claude with the trading-prompt system prompt and a strict JSON schema, so the response is guaranteed to be a valid `TradeAction`. **Each `(chat, topic)` pair is a continuous conversation thread**: the full message history is persisted in Redis and replayed on every call, with Anthropic's server-side compaction (`compact-2026-01-12`) summarizing earlier turns when the context grows large. Successful interpretations (excluding `no_action`) are republished to `trade-actions`. With `USE_DEEPSEEK=true` the same prompt goes to DeepSeek's OpenAI-compatible API instead: JSON-object mode plus Pydantic validation, history capped at `DEEPSEEK_MAX_TURNS` pairs, images dropped from history.

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

All variables are read from `.env` (see `.env.example`). "required" means there is no default.

| Variable | Purpose | Default |
|---|---|---|
| `TG_API_ID`, `TG_API_HASH` | Telegram app credentials (https://my.telegram.org/apps). Listener and `login.py`. | required |
| `TG_SESSION` | Path of the Telethon session file inside the listener container. | `/app/data/listener` |
| `TG_CHAT_ID` | Supergroup dialog ID (`-100…`) to listen on. Blank = all chats. | blank |
| `TG_TOPIC_ID` | Forum topic to listen on. Blank = all topics in the chat. | blank |
| `KAFKA_BOOTSTRAP` | Kafka bootstrap address, all three services. | `kafka:9092` (listener code falls back to `localhost:29092`) |
| `KAFKA_TOPIC` | Topic the listener writes and the consumer reads. | `telegram-events` |
| `KAFKA_ACTIONS_TOPIC` | Topic the consumer writes and the executor reads. | `trade-actions` |
| `KAFKA_GROUP_ID` | Consumer group of the consumer. | `telegram-consumer` |
| `EXECUTOR_GROUP_ID` | Consumer group of the executor. | `forex-executor` |
| `USE_DEEPSEEK` | `true` routes interpretation through DeepSeek instead of Anthropic. One backend per process. | `false` |
| `ANTHROPIC_API_KEY` | Claude API key, read by the Anthropic SDK. | required unless `USE_DEEPSEEK=true` |
| `ANTHROPIC_MODEL_TEXT` | Model for text-only turns. | `claude-haiku-4-5` |
| `ANTHROPIC_MODEL_IMAGE` | Model for turns that carry an image. | `claude-opus-4-7` |
| `DEEPSEEK_API_KEY` | DeepSeek API key. | required if `USE_DEEPSEEK=true` |
| `DEEPSEEK_BASE_URL` | DeepSeek endpoint. | `https://api.deepseek.com` |
| `DEEPSEEK_MODEL_TEXT` | DeepSeek model for text-only turns. | `deepseek-chat` |
| `DEEPSEEK_MODEL_IMAGE` | DeepSeek model for image turns. Must be vision-capable; the default is not. | `deepseek-chat` |
| `DEEPSEEK_MAX_TURNS` | User/assistant pairs of history kept and sent on the DeepSeek path. | `20` |
| `REDIS_URL` | Redis holding one conversation thread per `(chat, topic)`. | `redis://redis:6379/0` |
| `METAAPI_TOKEN`, `METAAPI_ACCOUNT_ID` | Broker bridge (https://app.metaapi.cloud/). | required |
| `DEFAULT_VOLUME` | Lot size of every order. | `0.01` |
| `ENABLE_INITIAL_SL_TP` | `true` sets a stop-loss and take-profit on a market order right after it fills. Off: only the trade manager's trailing logic applies. | off |

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

## Design decisions

- **Strict JSON schema on the Anthropic path.** `TradeAction` (Pydantic) is turned into a JSON Schema in which every field is required and nullable and `additionalProperties` is false, then passed as the output format. Why: every response parses into a valid `TradeAction`; the executor never sees free text. Trade-off: all 15 fields come back on every response, most of them `null`. The DeepSeek path only has JSON-object mode, so there the schema is checked afterwards by Pydantic and an invalid reply is dropped.
- **One conversation thread per `(chat, topic)`, compacted server-side.** The whole thread is stored under one Redis key and sent on every call; Anthropic's `compact-2026-01-12` beta summarises earlier turns. Why: signals refer back to earlier messages (levels from a chart, "close it"), and a sliding window of the last N messages forgets them. Trade-off: it depends on a beta API, and the DeepSeek path has no compaction, so it does fall back to a window (`DEEPSEEK_MAX_TURNS`).
- **Text model for text, vision model only for images.** `ANTHROPIC_MODEL_TEXT` answers text-only turns; `ANTHROPIC_MODEL_IMAGE` is used only when the turn carries an image. Why: a text signal does not need the vision model's price. Trade-off: one thread is answered by two models, each seeing history the other produced.
- **Kafka between the stages.** listener → `telegram-events` → consumer → `trade-actions` → executor, each reader in its own consumer group. Why: a stage restarts on its own, and committed offsets mean the executor does not re-read actions it already handled; `auto_offset_reset=latest` makes a group with no committed offset start at the end of the topic instead of replaying history. Trade-off: delivery is at-least-once, and actions published while the executor is down run when it comes back (see Caveats).

## Caveats

- `DEFAULT_VOLUME` is a single constant — no per-signal sizing yet, and `partial_close` currently closes the whole position.
- `PIP_VALUE` is a single constant (`0.01`) used for `update_takeprofit_from_pips`. Broker-specific — change it in `executor.py` if your broker quotes pips differently.
- All position-modification actions (`update_stoploss`, `update_takeprofit*`, `move_stop_to_entry`) act on every open position on the symbol. No per-position targeting.
- The Redis history cache is in-memory persistence (AOF) — fine on one box, replace with managed Redis for production.
- The executor resumes from its committed Kafka offset. Actions published while it is down are executed when it comes back, however old they are; there is no staleness check. `auto_offset_reset=latest` only applies when the group has no committed offset (first start, or offsets expired on the broker); only then is history skipped. Offsets are auto-committed, so an action handled just before a crash can be delivered a second time.
- The stored thread is never trimmed on the Anthropic path: Redis keeps every turn, including base64 images, and each request carries all of them. Compaction bounds the model's context, not the Redis value or the request size.
- Kafka and Redis bind only to the compose network — nothing is exposed to the host. To run a service on the host instead of in the container, expose the port temporarily or use `docker exec`.

## License

Use it, fork it, change it, ship it. No warranty, no restrictions, no attribution required. Do whatever you want with it.

If your country needs a formal license to make the above stick: this project is released under the MIT License, see [LICENSE](LICENSE).
