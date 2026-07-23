# Milestones

Per `PROJECT.md`'s Development Requirements: the project is built in
milestones, never all at once, with tests run and changes committed at the
end of each, and explicit approval required before starting the next one.

## Milestone 1 — Core Architecture ✅ complete

- Configuration system (YAML defaults + env var overrides, nothing hardcoded)
- Structured logging (console + rotating file + error-only file)
- Event Bus (async pub/sub, per-subscriber queues, backpressure, isolated handler failures)
- Universal Plugin Contract + auto-discovery loader + registry (isolates failed plugins)
- Universal Evidence Object (matches the spec's exact schema)
- Reasoning Engine (evidence accumulation, Claude provider, graceful evidence-only fallback)
- Database layer (async SQLAlchemy, Alembic, Repository pattern, no raw SQL, event_log persistence)
- One reference plugin end-to-end (`EMA` indicator — real EMA cross detection, not a stub)
- FastAPI app with `/health` and `/plugins`, wired to graceful shutdown via ASGI lifespan
- Docker Compose (Postgres + app, healthchecks, auto-restart, auto-migrate on boot)
- 35 tests, ~92% coverage of `app/`

Explicitly **not** in this milestone: Discord connection, the rest of the
indicator library, strategies, scanners, news/earnings/macro engines,
watchlists, backtesting, journaling, risk engine, coaching. Those are next.

## Milestone 2 — Discord Bot Skeleton ✅ complete

- `CommandInvoked` / `CommandFailed` events — commands are logged exactly
  like every other event (app/event_bus)
- `dispatch_command()` — framework-agnostic command execution: publishes
  the audit event, runs the plugin, isolates exceptions, never crashes the
  bot on a broken command (app/discord/dispatch.py)
- `DiscordCommandPlugin` — a new plugin base class (`commands` category)
  adding one method, `execute()`, on top of the Universal Plugin Contract;
  discovered by the exact same `PluginRegistry` as every other plugin
- `TradingBot` — thin discord.py adapter: built-in `/help`, auto-registers
  every discovered command plugin as a slash command, guild-scoped sync for
  instant dev iteration (app/discord/bot.py)
- Reference plugin: `/ping` — proves the full pipeline end to end, the same
  way EMA proved the indicator pipeline in Milestone 1
  (plugins/commands/ping)
- Bot lifecycle wired into `bootstrap()`/`teardown()` — graceful
  degradation if `DISCORD_BOT_TOKEN` isn't set (same pattern as the
  Reasoning Engine without an API key), graceful shutdown on SIGTERM,
  `/health` reports Discord connection state
- 46 tests passing, ~92% coverage of `app/`, ruff clean

**What wasn't (and couldn't be) verified here:** this sandbox has no
network path to Discord's gateway, so the actual `bot.start(token)` /
`on_ready` / live slash-command sync has to be verified on your machine.
Everything up to that boundary — command registration onto the real
discord.py `CommandTree`, the Interaction → `dispatch_command` bridge (with
a fake `Interaction`), event publishing, error isolation — is unit tested.
**Next step for you:** follow `docs/DISCORD_BOT_SETUP.md` if you haven't
already, put the token + guild ID in `.env`, run `docker compose up`, and
try `/ping` and `/help` in your VerserTrades server.

## Milestone 3 — Indicator Library ✅ complete

- `app/indicators/` — core (not a plugin) shared infrastructure every
  indicator plugin builds on, so no two plugins ever reimplement the same
  formula (`PROJECT.md`'s Indicator System: "No duplicate calculations"):
  - `bar.py` — `Bar`, bounded per-symbol `SymbolWindow` (default 300-bar
    rolling history), `bar_from_event()` (turns a `MarketDataUpdated` tick
    into a bar, degrading gracefully to open == high == low == close ==
    price when only tick data is available)
  - `math.py` — pure, stateless calculation functions (`sma`, `ema_step`/
    `ema_series`, `rsi`, `macd`, `atr`, `adx`, `bollinger_bands`,
    `donchian_channel`, `supertrend`, `obv`, `vwap`, `volume_profile`,
    `cci`, `ichimoku`) — every one returns `None` on insufficient history
    rather than raising
- `MarketDataUpdated` extended with optional `open`/`high`/`low`/`close`
  fields (non-breaking — tick-only plugins like EMA are unaffected) so
  bar-based indicators (ATR, ADX, Supertrend, Ichimoku, Donchian) have a
  real trading range to work with
- 13 new indicator plugins under `plugins/indicators/`, each following the
  `EMAPlugin` pattern and publishing edge-triggered evidence (a crossing or
  regime change, never "value is currently above X" on every tick): SMA,
  VWAP, RSI, MACD, ATR, ADX, Bollinger, Supertrend, OBV, CCI, Ichimoku,
  Donchian, Volume Profile — 14 indicators total including Milestone 1's
  EMA
- `EMAPlugin` refactored to import `ema_step` from `app.indicators.math`
  instead of defining its own copy — the same "no duplicate calculations"
  rule applied retroactively to the reference plugin
- Volume-dependent plugins (VWAP, OBV, Volume Profile) report a `degraded`
  health status rather than silently publishing all-zero evidence forever
  when the market data feed never carries real volume
- Fixed a test-isolation gap surfaced by this milestone: the suite now
  always shadows Discord/Anthropic/broker secrets with empty env vars
  (`tests/conftest.py`) so a real local `.env` — like the one created
  following `docs/DISCORD_BOT_SETUP.md` for Milestone 2's live
  verification — never changes what the test suite sees
- 111 tests passing (34 new math tests + 31 new plugin tests + 46 fixes/
  updates to existing tests for the larger registry), ~93% coverage of
  `app/`, ruff clean

**Design choices worth knowing about:** indicators that need history (RSI,
MACD, ATR, ADX, Supertrend) recompute over the retained rolling window each
update rather than carrying continuous incremental state — simpler to read,
test, and verify by hand, at the cost of a small EMA/Wilder "seed bias"
that's negligible once the window is several multiples of the indicator's
period (window defaults to 300 bars; the longest period in this milestone
is Ichimoku's 52). Some indicators encode a specific interpretation where
technical analysis has more than one school of thought — e.g. Bollinger and
CCI here use the breakout/continuation reading, not mean-reversion — and
say so in their docstrings so it's an explicit, visible choice rather than
a silent assumption.

## Milestone 4 — Strategy Engine + Evidence Aggregator ✅ complete

- **Evidence Aggregator** (`app/aggregation/`) — sits between every evidence
  producer and everything that consumes evidence; the single interface both
  the Strategy Engine and the Reasoning Engine subscribe to (neither
  subscribes to raw `EvidenceProduced` directly). Deduplicates repeated
  confirmations (keeping the occurrence count as metadata, never discarding
  the original events — full history stays queryable via `.history()`),
  decays evidence freshness linearly over `aggregation.freshness_window_seconds`
  (900s default), and flags conflicting bullish/bearish evidence rather than
  silently averaging it away. Publishes one `EvidenceAggregated` event per
  incoming `EvidenceProduced`, carrying the original evidence, its
  enrichment metadata, and the current deduped/fresh snapshot.
- **Strategy Engine** (`app/strategy/`, `plugins/strategies/`) — strategies
  are pure declarative YAML, never Python. `StrategyDefinition` (name,
  required/optional evidence titles, minimum_score, per-source
  repeat_policy) compiles once at load time into an immutable
  `CompiledStrategy` (frozensets + a score threshold — the "rule graph,"
  built once, not re-parsed per evaluation). Knows nothing about EMA, RSI,
  MACD, or any other indicator — only reads `Evidence.title`/`.source`/
  `.score`/`.direction`/`.metadata`, so a new indicator plugin's evidence is
  usable by any strategy immediately, with zero changes here. Subscribes to
  `EvidenceAggregated`, publishes `StrategyMatched` only on the
  not-matched → matched transition (edge-triggered, same rule every
  indicator plugin follows).
- **Reference strategy**: `plugins/strategies/momentum_breakout/strategy.yaml`
  — the same role `EMA` plays for indicators and `Ping` plays for Discord
  commands.
- **Donchian repeat_policy** (`plugins/indicators/donchian/`) — the plugin's
  own math is never suppressed (a sustained trend still fires a fresh "new
  high" breakout on every qualifying bar, which is mathematically correct).
  What's configurable is how often the *plugin* publishes evidence about it
  (`every_breakout` / `first_breakout` / `after_pullback`), and every
  occurrence — published or not — is tagged with sequence metadata
  (`breakout_sequence`, `bars_since_first_breakout`, `is_first_in_sequence`,
  `is_first_ever`, `distance_from_channel`). That metadata convention is
  also what the Strategy Engine's own `repeat_policy` filter reads, so a
  strategy can reinterpret repeats differently than the plugin's own
  publish policy — generic, not a Donchian special case.
- Reasoning Engine updated to consume `EvidenceAggregated` + `StrategyMatched`
  instead of raw `EvidenceProduced` — its synthesis now mentions matched
  strategies by name, and `suggested_strategies` reflects real matches
  instead of always being empty in evidence-only mode.
- `GET /strategies` — loaded strategy definitions (required/optional
  evidence, minimum score, repeat policy), mirroring `GET /plugins`.
- 144 tests passing (33 new: 8 aggregator, 17 strategy engine, 6 Donchian
  repeat-policy, 2 full-pipeline integration), ~94% coverage of `app/`,
  ruff clean. Live-verified end to end: real indicator plugins → real
  Evidence Aggregator → real Strategy Engine matching the real reference
  strategy → real Reasoning Engine synthesis, all in one run (see the
  Milestone 4 completion report for the transcript).

## Milestone 5 — `/analyze SYMBOL` ✅ complete

- **`DiscordCommandPlugin.parameters`** (`app/discord/command_plugin.py`) —
  a command plugin declares its slash-command options as data:
  `CommandOption(name, description, required)`, string-typed for now.
  `TradingBot._build_parameterized_callback()` dynamically builds a real
  function whose Python signature matches the declared options (discord.py
  derives a command's options by inspecting the callback's signature —
  there's no supported way to attach them otherwise), so a command with a
  real argument is still "drop a folder in," no other integration step.
  Option names are validated the same way command names are
  (`is_valid_option_name`); an invalid one is logged and skipped, not
  fatal. A zero-parameter command (`/ping`, `/help`) is unaffected.
- **`CommandResponse.buttons`** (`app/discord/dispatch.py`) —
  `CommandButton(label, custom_id, style, disabled)`, a plain dataclass so
  a command plugin declaring buttons stays testable without discord.py.
  `TradingBot` turns these into a real `discord.ui.View` when sending the
  response. `custom_id` convention: `"{command}:{action}:{extra}"` — the
  adapter's click handler treats the action `"dismiss"` specially (deletes
  the message) and gives every other action a generic, honest "not built
  yet" reply, since the systems some buttons imply don't exist yet. This
  is generic, not `/analyze`-specific — any future command reusing
  `dismiss` gets the same working behavior for free.
- **`PluginContext` read-only query exception** (`app/plugins/base.py`,
  `app/plugins/registry.py`, `app/core/bootstrap.py`) — three new optional
  fields, `reasoning_engine` / `evidence_aggregator` / `strategy_engine`,
  default `None`, threaded through `PluginRegistry` from `bootstrap()`. A
  deliberate, narrow, documented exception to "plugins only talk through
  the Event Bus": a command plugin needing the *current* state on demand
  (not the next event) may read from these, never mutate them, never
  reach into a specific indicator plugin.
- **Reference plugin: `/analyze SYMBOL`**
  (`plugins/commands/analyze/`) — the same role `EMA` plays for
  indicators, `Ping` plays for commands, and `Momentum Breakout` plays for
  strategies. Pulls the Evidence Aggregator's current snapshot and the
  Reasoning Engine's current output for a symbol, formats them into one
  message, and attaches all seven buttons (Chart / News / History /
  Backtest / Journal / Watch / Dismiss) — only Dismiss has real behavior
  today, honestly, since the other six name systems (charting, news,
  history, backtesting, journaling, watchlists) that aren't built yet.
- 15 new tests (CommandOption/CommandButton defaults and validation,
  parameterized registration deriving a real discord.py option, callback
  arg-passing, invalid-option isolation, button-view construction, dismiss
  behavior, placeholder-button behavior, and 5 `/analyze`-specific tests
  against real `EvidenceAggregator`/`StrategyEngine`/`ReasoningEngine`
  instances) plus one existing test updated for the new registered
  command; 159 tests passing total, ~94% coverage of `app/`, ruff clean.
- **Known limitation, not a bug:** there's no live market data feed yet
  (that's the Scanner Engine, next up) — `/analyze` for any real-world
  symbol will honestly report `insufficient_evidence` until something has
  actually published `MarketDataUpdated` for it. Same graceful-degradation
  pattern the Reasoning Engine already uses everywhere else.

## Milestone 6 — Scanner Engine + Market Data Abstraction Layer + Discord Action Registry ✅ complete

- **Market Data Abstraction Layer** (`app/marketdata/`, `plugins/market_data/`)
  — the Scanner Engine never talks to a specific data source; only to
  `MarketDataService`. `MarketDataProviderPlugin` (one method, `fetch(symbols,
  timeframe) -> dict[str, Bar]`, on top of the Universal Plugin Contract) is
  a plugin category exactly like indicators or commands. `MarketDataService`
  is built once provider plugins have loaded, from a priority-ordered
  `settings.market_data.providers` list, and does real multi-provider
  failover: a provider that raises or doesn't have a symbol yet is skipped,
  and the next configured provider is asked for whatever's still missing.
  **Reference provider**: `plugins/market_data/replay/` (`ReplayProviderPlugin`)
  — CSV replay (`{data_dir}/{SYMBOL}.csv`, looping once exhausted) with a
  deterministic (seeded) synthetic-random-walk fallback for any symbol
  without a CSV file, so the pipeline runs with zero external setup. Both
  data sources are honestly labeled as development/replay data, never
  presented as real market data.
- **Scanner Engine** (`app/scanner/`, `plugins/scanners/`) — the first
  continuous, always-on system in the platform. `ScannerPlugin` implements
  the Universal Plugin Contract generically: `initialize()` starts a real
  `asyncio` background task that calls `scan_once()` on a loop
  (`interval_seconds` apart), fetching from `MarketDataService` (never a
  specific provider) and publishing `MarketDataUpdated` per symbol/
  timeframe — **never** calling an indicator plugin, the Evidence
  Aggregator, or the Strategy Engine directly; everything downstream
  discovers new data by subscribing to the Event Bus, exactly like any
  other source of that event. A failed tick is logged, reflected in
  `health()` as `degraded`, and retried next interval — never crashes the
  process. Concrete scanners are almost entirely configuration (watchlist,
  timeframes, interval, asset class), which is what makes "multiple
  watchlists" and "multiple scanners simultaneously" true with zero shared-
  logic changes: another `plugins/scanners/<name>/config.yaml` is a second,
  independent scanner. **Reference scanner**: `plugins/scanners/core/`
  (`CoreWatchlistScanner`).
- **`PluginContext` gains `market_data_service` and `plugin_registry`**
  (`app/plugins/base.py`) — the same documented, narrow, read-only-query
  exception introduced in Milestone 5, extended to cover a scanner's
  on-demand market data read and `/scan`'s introspection of what's
  currently loaded. Plugin loading is now two explicit phases
  (`app/core/bootstrap.py`): market data providers load first, then
  `MarketDataService` is built from the result and handed to the registry,
  then everything else (indicators, commands, scanners, ...) loads with a
  real service already available.
- **Discord Action Registry** (`app/discord/actions.py`) — centralizes what
  Milestone 5 left per-command: button creation, click-callback
  registration, shared styling, and placeholder behavior for the
  platform's reusable actions (chart/news/history/backtest/journal/watch/
  refresh/replay/coach/dismiss). A command plugin now declares which
  actions it wants (`ACTION_REGISTRY.buttons_for(["chart", "watch",
  "dismiss"], target=symbol)`) instead of constructing `CommandButton`s or
  implementing click behavior. `custom_id` convention changed to
  `"{action_key}:{target}"` (action-first, command-agnostic, superseding
  Milestone 5's `"{command}:{action}:{extra}"`) so the same button behaves
  identically regardless of which command attached it. `"dismiss"` is the
  one action with a real handler today; giving any other action real
  behavior later is one `ACTION_REGISTRY.register_handler(key, handler)`
  call here, with zero changes to any command plugin already using it. A
  documented (currently no-op) `requires_permission`/`check_permission`
  seam exists for a future role/permission system.
- **`/scan`** (`plugins/commands/scan/`) — zero-parameter status command
  reporting every loaded scanner's watchlist/timeframes/interval/health and
  the configured market data provider(s), using the same Action Registry
  (Refresh / Dismiss) `/analyze` uses — concrete proof the registry is
  reusable across commands, not `/analyze`-specific.
- **`GET /scanners`** — mirrors `GET /plugins`/`GET /strategies`: loaded
  scanner plugins plus configured market data providers.
- 40 new tests (10 Market Data Abstraction Layer, 13 Scanner Plugin, 15
  Action Registry, 4 `/scan` command, 1 full continuous-scanning-to-
  `/analyze` integration test using a real `ScannerPlugin` ticking on a
  real background loop over real compressed wall-clock time — no
  hand-published `MarketDataUpdated`/`EvidenceProduced` anywhere in that
  test) plus 4 existing tests updated for the new custom_id convention and
  the two new registered commands (`/analyze`, `/scan`, `/ping`, `/help`);
  199 tests passing total, ~94% coverage of `app/` (maintained from
  Milestone 5), ruff clean. The reference scanner is disabled by default in
  the test suite's `settings` fixture (`tests/conftest.py`) so it doesn't
  spin up an unwanted background task in the ~150 other tests that load the
  full plugin registry.
- Live-verified end to end: a real `ScannerPlugin` (short interval) reading
  a real `ReplayProviderPlugin` (synthetic random walk) → real indicator
  plugins → real Evidence Aggregator → real Strategy Engine → `/analyze`
  reflecting live-generated evidence, plus `/scan` reporting real scanner
  health and `/analyze`'s buttons routed entirely through the Action
  Registry (see the Milestone 6 completion report for the transcript).

## Proposed order for what's next

These map directly to `PROJECT.md` sections. Suggested build order —
open to reordering based on what you want to see working first:

1. **News / Earnings / Macro engines** — each a plugin category, each only
   ever publishing `NewsReceived` / `EarningsReleased` / evidence, never a
   directive.
2. **A real market data provider** (Polygon/Alpaca/Finnhub/similar) —
   implements `MarketDataProviderPlugin` exactly like `ReplayProviderPlugin`
   does; add it to `settings.market_data.providers` and the Scanner Engine
   picks it up with zero changes. Needs a real API credential this
   environment doesn't have, so it's a "when you're ready" item, not a
   blocker for anything else.
3. **Watchlists**, then **Backtesting**, then **Journaling**, **Risk
   Engine**, **AI Coach**, **Replay Mode**, **Optimization Engine**,
   **Personal Statistics** — roughly in that order, since each leans on the
   ones before it (backtesting needs strategies + indicators; the coach
   needs journaling; risk warnings need trade events already flowing).
   These are also what would give the Action Registry's Chart / News /
   History / Backtest / Journal / Watch / Replay / Coach actions real
   behavior instead of a placeholder reply — each is a single
   `ACTION_REGISTRY.register_handler()` call once the backing system
   exists.

Say the word and the next milestone starts. Nothing here commits to a
specific order — just say which one you want first.
