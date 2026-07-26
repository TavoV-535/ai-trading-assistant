# AI Trading Assistant

An event-driven, plugin-first Discord trading intelligence platform. It
gathers market evidence, reasons about it with Claude, explains its
conclusions, and helps you test and improve strategies — it is explicitly
**not** a signal-selling bot. See [`PROJECT.md`](./PROJECT.md) for the full
product spec this codebase is built against.

Runs entirely on your own machine via Docker Compose.

## Status

**Milestones 1-11 complete: Core Architecture, Discord Bot Skeleton, the
Indicator Library, the Strategy Engine + Evidence Aggregator,
`/analyze SYMBOL`, the Scanner Engine + Market Data Abstraction Layer
+ Discord Action Registry, the External Intelligence Platform +
Market Context Engine + Confidence Weighting Framework, the
Portfolio & Watchlist Intelligence Layer + Event Prioritization Engine,
the Unified Simulation Engine + Decision Timeline, the Unified
Trading Journal + Reflection Engine, and the Capital Protection Engine +
Adaptive Risk Profile system.**

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
evidence (Milestone 7); and now the platform is proactive, not just
reactive — a Portfolio Intelligence Layer continuously profiles every
configured watchlist symbol and ranks them by a transparent priority score
(`/watchlist`), while an independent Event Prioritization Engine scores
every candidate development for importance, novelty, confidence change,
urgency, and user relevance before deciding whether it's worth a real,
duplicate-suppressed Discord alert — reducing notification fatigue without
missing what actually matters (Milestone 8); and now a Unified Simulation
Engine (`app/simulation/`) can drive the exact same pipeline — every
engine, every event, no shortcuts — against historical data instead of
live data, deterministically, while a new Decision Timeline
(`app/timeline/`) records the complete reasoning behind every simulated
decision (context, evidence, confidence weights, matched strategies, a
non-directive `watch_*` hypothesis label, and a retroactively resolved
outcome) as the canonical historical record future Replay Mode,
Journaling, AI Coach, and Explainability features will consume (Milestone
9); and now every resolved decision automatically gets a structured
post-trade analysis — a new Reflection Engine (`app/reflection/`)
publishes a `ReflectionGenerated` event (why the decision was made,
supporting/contradictory evidence, market context, confidence evolution,
outcome, lessons learned, potential improvements) purely from the event
bus, and a new Trading Journal (`app/journal/`) independently enriches —
never duplicates — the Decision Timeline's own records with that
reflection plus user notes and screenshot placeholders, retrievable via a
new `/journal SYMBOL` command (Milestone 10); and now capital preservation
is modeled as continuously evolving state, not a threshold check — a new
Capital Protection Engine (`app/capital_protection/`) observes every
`DecisionRecorded` (synthesizing the standard `TradeOpened`/`TradeClosed`
vocabulary itself), maintains a running equity curve, and publishes
structured `RiskEvent`s for daily/total/trailing drawdown, consecutive
losses, open portfolio risk, position/symbol/sector concentration, real
Pearson-correlated exposure, and prop-firm compliance (margin/broker
constraints are honestly reported as not-yet-applicable placeholders) —
never blocking a trade or command, only publishing to the event bus for
Discord, the Trading Journal, Portfolio Intelligence, and the future AI
Coach to independently consume. A parallel Adaptive Risk Profile system
(`app/capital_protection/profiles.py`) defines five built-in profiles
(Conservative, Swing Trader, Day Trader, Scalper, Prop Firm) plus runtime
Custom Profiles, switchable live via `/risk profile:<name>` with zero code
changes (Milestone 11). See [`docs/MILESTONES.md`](./docs/MILESTONES.md)
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
curl http://localhost:8000/watchlist
```

In Discord, try `/ping`, `/help`, `/scan` (what the Scanner Engine is
currently watching), `/analyze SYMBOL`, `/watchlist` (the Portfolio
Intelligence Layer's ranked, prioritized view of every configured symbol),
`/journal SYMBOL` (the Trading Journal's enriched decision history —
empty in live mode today, since nothing yet publishes `DecisionRecorded`
outside a Simulation Engine run; see `docs/ARCHITECTURE.md`'s "Trading
Journal" section), and `/risk [profile]` (the Capital Protection Engine's
current status with no argument, or switches the active Risk Profile when
given one — also empty of real risk events in live mode today for the same
reason `/journal` is, since nothing yet publishes `DecisionRecorded`
outside a Simulation Engine run) — the reference scanner watches NVDA/AAPL/TSLA against
the bundled
synthetic-random-walk data provider by default (the same three symbols
`portfolio.watchlist` tracks out of the box), so `/analyze NVDA` and
`/watchlist` should both show real, continuously-generated evidence within
a few seconds of the app starting. Any other symbol reports
`insufficient_evidence` until a scanner is configured to watch it, and
`/watchlist` only ever tracks symbols listed in `portfolio.watchlist`
(`config/default.yaml`). Note: the reference News/Earnings/Macro
intelligence plugins and the reference scanner are enabled by default in a
real deployment (only disabled in the test suite) — give it a minute or
two and `/analyze NVDA`'s response will also show a **Market context**
line, weighted evidence from the Confidence Weighting Framework, and (once
its priority score moves enough to matter) a **Watchlist priority** line.
Set `discord.alert_channel_id` in `config/default.yaml` (or override via
settings) to have the Event Prioritization Engine post real alerts to a
channel as they're generated, instead of only logging its decisions.

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

455 tests, ~95% coverage of `app/` as of Milestone 11. Live Discord gateway
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
  core/         # bootstrap/teardown sequencing + FastAPI app (/health, /plugins, /strategies, /scanners, /watchlist)
  indicators/   # shared calculation library every indicator plugin uses (not a plugin itself)
  aggregation/  # Evidence Aggregator + Confidence Weighting Framework (not a plugin itself)
  strategy/     # Strategy Engine — compiles declarative YAML into a rule graph (not a plugin itself)
  marketdata/   # Market Data Abstraction Layer — provider-agnostic fetch() + failover (not a plugin itself)
  scanner/      # Scanner Plugin base — the continuous tick loop every scanner plugin shares
  intelligence/ # IntelligencePlugin base — the shared External Intelligence Platform contract
  context/      # Market Context Engine — derives Bull/Bear Trend, volatility, Risk-On/Off, ... (not a plugin itself)
  portfolio/    # Portfolio Intelligence Layer — per-symbol profiles + ranked priority scoring (not a plugin itself)
  prioritization/ # Event Prioritization Engine — scores/gates candidate developments into real alerts (not a plugin itself)
  simulation/   # Unified Simulation Engine — drives the full pipeline against historical data (not a plugin itself)
  timeline/     # Decision Timeline — canonical historical record of every recorded decision (not a plugin itself)
  reflection/   # Reflection Engine — automatic structured post-trade analysis per resolved decision (not a plugin itself)
  journal/      # Trading Journal — enriches Decision Timeline records with reflections/notes/screenshots (not a plugin itself)
  capital_protection/ # Capital Protection Engine + Adaptive Risk Profile system — continuously-evolving capital-preservation risk state, never blocks trades (not a plugin itself)
plugins/        # actual plugins/strategies live here, auto-discovered — see docs/PLUGIN_GUIDE.md
  indicators/   # ema, sma, vwap, rsi, macd, atr, adx, bollinger, supertrend, obv, cci, ichimoku, donchian, volume_profile
  strategies/   # momentum_breakout/strategy.yaml (pure YAML, no Python)
  commands/     # ping/, analyze/, scan/, watchlist/, journal/, risk/
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
