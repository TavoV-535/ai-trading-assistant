# AI Trading Assistant

An event-driven, plugin-first Discord trading intelligence platform. It
gathers market evidence, reasons about it with Claude, explains its
conclusions, and helps you test and improve strategies — it is explicitly
**not** a signal-selling bot. See [`PROJECT.md`](./PROJECT.md) for the full
product spec this codebase is built against.

Runs entirely on your own machine via Docker Compose.

## Status

**Milestones 1-7 complete: Core Architecture, Discord Bot Skeleton, the
Indicator Library, the Strategy Engine + Evidence Aggregator,
`/analyze SYMBOL`, the Scanner Engine + Market Data Abstraction Layer
+ Discord Action Registry, and the External Intelligence Platform +
Market Context Engine + Confidence Weighting Framework.**

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
(Milestone 4); `/analyze SYMBOL` is the first command with a real
parameter, pulling the current evidence + reasoning state for a symbol
into an interactive message with buttons (Milestone 5); the platform
became continuous end to end — a Scanner Engine ticks on a real background
loop, reading market data only through a provider-agnostic Market Data
Abstraction Layer (a CSV-replay/synthetic-random-walk reference provider
ships today; a real live feed is a future provider plugin, zero Scanner
Engine changes needed), while a centralized Discord Action Registry gives
`/analyze` and the `/scan` status command consistent, reusable buttons
instead of each command building its own (Milestone 6); and now every
non-price source of market information — News, Earnings, Macro, and any
future source — is a plugin on one unified External Intelligence
Platform, never a separate isolated engine; a new Market Context Engine
derives real market-environment labels (Bull/Bear Trend, High/Low
Volatility, Gap Day, Trend Exhaustion, Low Liquidity, market-wide
Risk-On/Risk-Off, Fed Week/CPI Day/Earnings Season) from real price and
intelligence data; and a Confidence Weighting Framework computes a
transparent, normalized weight for every piece of evidence — freshness,
persistence, source reliability, market regime, cross-confirmation,
contradiction, and more — without ever hiding or replacing the original
evidence (Milestone 7). See [`docs/MILESTONES.md`](./docs/MILESTONES.md)
for what's done and what's next.

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
curl http://localhost:8000/scanners
```

In Discord, try `/ping`, `/help`, `/scan` (what the Scanner Engine is
currently watching), and `/analyze SYMBOL` — the reference scanner watches
NVDA/AAPL/TSLA against the bundled synthetic-random-walk data provider by
default, so `/analyze NVDA` should show real, continuously-generated
evidence within a few seconds of the app starting. Any other symbol
reports `insufficient_evidence` until a scanner is configured to watch it.
Note: the reference News/Earnings/Macro intelligence plugins and the
reference scanner are enabled by default in a real deployment (only
disabled in the test suite) — give it a minute or two and `/analyze
NVDA`'s response will also show a **Market context** line and weighted
evidence from the Confidence Weighting Framework, not just raw technical
evidence.

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

262 tests, ~95% coverage of `app/` as of Milestone 7. Live Discord gateway
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
  discord/      # TradingBot + DiscordCommandPlugin contract + command dispatch + Action Registry
  reasoning/    # Reasoning Engine + Claude provider
  db/           # SQLAlchemy models, Repository pattern, event persistence
  core/         # bootstrap/teardown sequencing + FastAPI app (/health, /plugins, /strategies, /scanners)
  indicators/   # shared calculation library every indicator plugin uses (not a plugin itself)
  aggregation/  # Evidence Aggregator + Confidence Weighting Framework (not a plugin itself)
  strategy/     # Strategy Engine — compiles declarative YAML into a rule graph (not a plugin itself)
  marketdata/   # Market Data Abstraction Layer — provider-agnostic fetch() + failover (not a plugin itself)
  scanner/      # Scanner Plugin base — the continuous tick loop every scanner plugin shares
  intelligence/ # IntelligencePlugin base — the shared External Intelligence Platform contract
  context/      # Market Context Engine — derives Bull/Bear Trend, volatility, Risk-On/Off, ... (not a plugin itself)
plugins/        # actual plugins/strategies live here, auto-discovered — see docs/PLUGIN_GUIDE.md
  indicators/   # ema, sma, vwap, rsi, macd, atr, adx, bollinger, supertrend, obv, cci, ichimoku, donchian, volume_profile
  strategies/   # momentum_breakout/strategy.yaml (pure YAML, no Python)
  commands/     # ping/, analyze/, scan/
  market_data/  # replay/ (CSV replay + synthetic random-walk reference provider)
  scanners/     # core/ (reference watchlist scanner)
  intelligence/ # news/, earnings/, macro/ (External Intelligence Platform reference plugins)
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
