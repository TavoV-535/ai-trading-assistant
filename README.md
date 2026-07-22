# AI Trading Assistant

An event-driven, plugin-first Discord trading intelligence platform. It
gathers market evidence, reasons about it with Claude, explains its
conclusions, and helps you test and improve strategies — it is explicitly
**not** a signal-selling bot. See [`PROJECT.md`](./PROJECT.md) for the full
product spec this codebase is built against.

Runs entirely on your own machine via Docker Compose.

## Status

**Milestones 1-5 complete: Core Architecture, Discord Bot Skeleton, the
Indicator Library, the Strategy Engine + Evidence Aggregator, and
`/analyze SYMBOL`.**

The event bus, plugin contract, evidence object, reasoning engine,
database layer, and local deployment are built (Milestone 1); the Discord
bot connects, exposes `/help` and a reference `/ping` command, and routes
every command through the same event-driven, plugin-first architecture
(Milestone 2); 14 indicator plugins (EMA, SMA, VWAP, RSI, MACD, ATR, ADX,
Bollinger, Supertrend, OBV, CCI, Ichimoku, Donchian, Volume Profile) share
one calculation library and publish evidence, never a signal (Milestone 3);
an Evidence Aggregator normalizes/dedupes/decays that evidence for two
downstream consumers — a declarative, YAML-only Strategy Engine (knows
nothing about any specific indicator) and the Reasoning Engine, which
mentions matched strategies by name in its non-directive synthesis
(Milestone 4); and `/analyze SYMBOL` is the first command with a real
parameter, pulling the current evidence + reasoning state for a symbol
into an interactive message with buttons (Milestone 5). See
[`docs/MILESTONES.md`](./docs/MILESTONES.md) for what's done and what's
next.

## Quick start (Docker — recommended)

```bash
cp .env.example .env
# fill in ANTHROPIC_API_KEY if you want AI-generated summaries;
# without it the Reasoning Engine still runs in evidence-only mode.
# fill in DISCORD_BOT_TOKEN + DISCORD_GUILD_ID to bring the bot online —
# see docs/DISCORD_BOT_SETUP.md if you haven't created the bot yet.
# without a token the app still runs fine, just without Discord.

./scripts/start.sh
# equivalent to: docker compose -f docker/docker-compose.yml up --build
```

This starts Postgres, runs Alembic migrations automatically, starts the
app, and (if a token is set) connects the Discord bot. Check it's alive:

```bash
curl http://localhost:8000/health   # includes "discord": "connected" | "connecting" | "not_configured"
curl http://localhost:8000/plugins
curl http://localhost:8000/strategies
```

In Discord, try `/ping`, `/help`, and `/analyze SYMBOL` (reports
`insufficient_evidence` for any symbol until a market data feed exists —
see `docs/MILESTONES.md`).

Stop everything with `./scripts/stop.sh`.

## Quick start (local, no Docker)

Requires a Postgres reachable at `DATABASE_URL` — the fastest way to get one
is `docker compose -f docker/docker-compose.yml up postgres`.

```bash
./scripts/dev.sh
```

## Configuration

Non-secret behavior lives in [`config/default.yaml`](./config/default.yaml).
Secrets and per-environment values live in `.env` (copy from
`.env.example`). Environment variables always win over the YAML file. See
`app/config/settings.py` for the full schema.

Nothing in this codebase reads `os.environ` directly — everything goes
through `app.config.get_settings()`.

## Testing

```bash
pip install -e ".[dev]"
pytest                              # full suite
pytest --cov=app --cov-report=term-missing   # with coverage
```

159 tests, ~94% coverage of `app/` as of Milestone 5. Live Discord gateway
connection can't be exercised in CI/sandboxes — see
[`docs/MILESTONES.md`](./docs/MILESTONES.md) for what's unit tested vs.
what needs verifying against a real Discord connection on your machine.

## Project structure

```
app/
  config/       # pydantic-settings: YAML + env vars, never hardcoded
  logging/      # structlog + rotating file handlers
  event_bus/    # the async pub/sub bus + every core Event schema
  evidence/     # the Universal Evidence Object
  plugins/      # PluginBase contract + auto-discovery + registry
  discord/      # TradingBot + DiscordCommandPlugin contract + command dispatch
  reasoning/    # Reasoning Engine + Claude provider
  db/           # SQLAlchemy models, Repository pattern, event persistence
  core/         # bootstrap/teardown sequencing + FastAPI app (/health, /plugins, /strategies)
  indicators/   # shared calculation library every indicator plugin uses (not a plugin itself)
  aggregation/  # Evidence Aggregator — dedup/freshness/conflict detection (not a plugin itself)
  strategy/     # Strategy Engine — compiles declarative YAML into a rule graph (not a plugin itself)
plugins/        # actual plugins/strategies live here, auto-discovered — see docs/PLUGIN_GUIDE.md
  indicators/   # ema, sma, vwap, rsi, macd, atr, adx, bollinger, supertrend, obv, cci, ichimoku, donchian, volume_profile
  strategies/   # momentum_breakout/strategy.yaml (pure YAML, no Python)
  commands/     # ping/, analyze/ (the first command with a real parameter)
alembic/        # migrations (async, driven by app.config settings)
docker/         # Dockerfile, docker-compose.yml, entrypoint.sh
docs/           # architecture, plugin guide, milestone tracker, Discord setup
tests/          # pytest suite mirroring the app/ layout
```

## Documentation

- [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) — how the event bus, plugin contract, evidence object, reasoning engine, and Discord bot fit together
- [`docs/PLUGIN_GUIDE.md`](./docs/PLUGIN_GUIDE.md) — how to add a new plugin (indicator or Discord command) without touching core code
- [`docs/DISCORD_BOT_SETUP.md`](./docs/DISCORD_BOT_SETUP.md) — creating the bot application and getting a token
- [`docs/MILESTONES.md`](./docs/MILESTONES.md) — what's built, what's next, in the order `PROJECT.md` implies
- [`PROJECT.md`](./PROJECT.md) — the full product spec
