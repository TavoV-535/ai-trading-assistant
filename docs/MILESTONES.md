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

## Milestone 7 — External Intelligence Platform + Market Context Engine + Confidence Weighting Framework ✅ complete

- **External Intelligence Platform** (`app/intelligence/`,
  `plugins/intelligence/`) — per PROJECT.md's explicit instruction, one
  unified plugin category, not separate isolated News/Earnings/Macro/SEC/
  Insider/Economic-Calendar engines. `IntelligencePlugin` implements the
  Universal Plugin Contract generically plus a config-driven polling loop
  (mirroring `ScannerPlugin`'s tick loop) and a `_publish(event, evidence)`
  helper that keeps every intelligence plugin's (Intelligence Event,
  Evidence) pair from drifting out of sync. **Three independent reference
  plugins**, each its own file sharing only the base contract:
  `News` (`NewsReceived`), `Earnings` (`EarningsReleased`, with a
  cross-symbol "earnings season" heuristic when multiple watchlist symbols
  report together), `Macro` (`MacroEventOccurred`, always market-wide —
  Fed meetings, CPI releases, jobs reports, treasury auctions, holiday
  sessions). Like `ReplayProviderPlugin` (Milestone 6), none call a real
  external API — deterministic, clearly-labeled synthetic data, honestly
  documented as such, never presented as real. Every macro/calendar event
  carries `metadata["context_hint"]`, a generic convention the Market
  Context Engine reads without knowing which plugin published it.
- **Market Context Engine** (`app/context/`) — a new core system (not a
  plugin) that derives higher-level market-environment labels from real,
  computed signals: Bull/Bear Trend and Sideways Market (rolling-window %
  change), High/Low Volatility (rolling-window return stdev), Gap Day
  (large jump between consecutive updates), Trend Exhaustion (decelerating
  second half of the trend window), Low Liquidity (volume vs. trailing
  average), market-wide Risk-On/Risk-Off (a genuine cross-symbol
  aggregate — majority of tracked symbols in the same trend direction),
  and macro/calendar context (Fed Week, CPI Day, Earnings Season, ... —
  promoted from intelligence evidence's `context_hint`, generic and
  extensible). Publishes `MarketContextUpdated`, edge-triggered per
  `(symbol, context_type)` — never spams the bus on every tick a label
  continues to hold. Never calls the Evidence Aggregator, Strategy Engine,
  or Reasoning Engine directly (checked structurally in
  `tests/test_milestone7_pipeline_integration.py`) — only
  `MarketContextUpdated`, only through the Event Bus.
- **Confidence Weighting Framework** (`app/aggregation/weighting.py`) —
  extends the Evidence Aggregator with a normalized `[0, 1]` weight (plus
  a fully transparent `breakdown` dict) per active piece of evidence,
  considering source/historical reliability (config, per source),
  freshness, persistence, timeframe alignment, cross-indicator
  confirmation, contradictory evidence, market regime (reads the Context
  Engine's current trend label via `MarketContextUpdated`), a documented
  correlation-dampening proxy, and an explicit no-op seam for future ML
  adjustments. Every factor is independently unit tested. **The original
  Evidence objects are never modified, replaced, or discarded** —
  `EvidenceAggregated.weighted_evidence` and
  `AggregateSnapshot.weighted_evidence` are always parallel, explainable
  annotations alongside the unweighted `active_evidence`.
- **Reasoning Engine updated** (`app/reasoning/engine.py`) — subscribes to
  `MarketContextUpdated` in addition to `EvidenceAggregated`/
  `StrategyMatched`. AI-mode prompts now carry each evidence item's
  `confidence_weight` and a "current market context" section; evidence-
  only-mode lean/confidence are computed from weighted mass when weighted
  evidence is available (not just raw counts), and the summary text names
  the current context labels. `ReasoningOutput.context` exposes exactly
  what context was used, for transparency.
- **`/analyze` updated** (`plugins/commands/analyze/`) — now reads
  `context.context_engine` (a new `PluginContext` field, the same
  documented read-only-query exception) alongside the existing
  `evidence_aggregator`/`reasoning_engine`, and renders all four Milestone
  7 dimensions in one response: technical + fundamental evidence counts, a
  **Market context** line (market-wide + symbol-specific labels), and the
  top confidence-weighted evidence with its framework-computed weight.
- `config/default.yaml` gains `plugins/intelligence` in
  `plugins.search_paths` (replacing the unused `plugins/news`/
  `plugins/earnings`/`plugins/macro` stubs from Milestone 1's scaffolding)
  plus three new tunable sections: `intelligence` (default poll interval),
  `context` (every Market Context Engine threshold), and
  `confidence_weighting` (per-source reliability + every weighting
  factor's tunable constant).
- 63 new tests (7 IntelligencePlugin base contract, 4 News, 3 Earnings, 2
  Macro, 17 Market Context Engine, 16 Confidence Weighting Framework math,
  5 Evidence Aggregator weighting integration, 5 Reasoning Engine
  context/weighting, 2 `/analyze` context display, 1 full Milestone 7
  pipeline integration test proving all four dimensions reach `/analyze`
  together, 2 architectural import-guardrail tests, 1 config test) plus
  existing tests updated (`tests/conftest.py` now also disables News/
  Earnings/Macro by default, same reasoning as the Milestone 6 scanner
  fix); 262 tests passing total, ~95% coverage of `app/` (improved from
  Milestone 6's ~94%), ruff clean.
- Live-verified end to end: real indicator plugins + real News/Earnings/
  Macro intelligence plugins + a real Market Context Engine (deriving a
  genuine "Bull Trend" from the same validated bar sequence
  `tests/test_pipeline_integration.py` uses) + the Confidence Weighting
  Framework producing genuinely different weights per evidence item, all
  flowing into a real `/analyze NVDA` response citing the matched
  "Momentum Breakout" strategy, the derived market context, and the
  weighted evidence — the same `AnalyzePlugin` the Discord bot runs (see
  `tests/test_milestone7_pipeline_integration.py`).

## Milestone 8 — Portfolio & Watchlist Intelligence Layer + Event Prioritization Engine ✅ complete

- **Portfolio Intelligence Layer** (`app/portfolio/`) — a new core system
  (not a plugin, the same tier as the Evidence Aggregator / Market Context
  Engine), continuously monitoring every symbol on `settings.portfolio.watchlist`
  and maintaining an evolving `SymbolProfile` per symbol: active/bullish/
  bearish/neutral evidence counts, the Confidence Weighting Framework's
  `top_weight`/`avg_weight` (reused directly, never recomputed), fundamental
  (News/Earnings/Macro) evidence freshness, current Market Context Engine
  labels, a computed `confidence_trend` (rising/falling/stable/unknown,
  from a rolling weight-history window), matched strategies, and historical
  alert state (last alert time, count). `app/portfolio/scoring.py::compute_priority()`
  turns this into a transparent `[0, 100]` `priority_score` + `breakdown`:
  evidence strength, fundamental freshness, market-context intensity
  (capped), confidence trend, strategy match, and an alert-suppression
  *dampening* factor (never a hard zero) for symbols alerted on recently —
  the watchlist keeps surfacing genuinely important symbols instead of
  re-spamming the same just-alerted one at the top. Publishes
  `SymbolProfileUpdated`, edge-triggered on a meaningful (≥0.5) score
  change. `snapshot(symbol)` / `ranked_watchlist()` are read-only, deep-copy
  query methods. Only tracks symbols in the configured watchlist — a symbol
  outside it is never profiled, so "watch a new symbol" is a config change,
  never a code change. Never imports the Evidence Aggregator, Strategy
  Engine, Reasoning Engine, or Event Prioritization Engine directly (checked
  structurally in `tests/test_milestone8_pipeline_integration.py`) — only
  the Event Bus.
- **Event Prioritization Engine** (`app/prioritization/`) — another new
  core system, sitting between the Evidence Aggregator (plus the Strategy
  Engine and Market Context Engine) and user notifications. Every candidate
  development — fresh evidence, a strategy match, a context shift — is
  scored by `app/prioritization/scoring.py::compute_alert_score()`:
  source-specific importance (a strategy match starts from a high flat
  base; a context shift's base depends on whether it's inherently
  high-stakes like Gap Day/Risk-Regime/macro events vs. routine; raw
  evidence's base scales with the Confidence Weighting Framework's own
  weight for it), novelty (occurrence-based for evidence; strategy/context
  candidates are already edge-triggered upstream, so always fully novel),
  a confidence-trend bonus (reads the Portfolio Intelligence Layer's
  cached trend for that symbol — rising/falling only, "stable" contributes
  nothing), a documented urgency proxy, and a flat watchlist-relevance
  bonus. Only a candidate clearing `prioritization.alert_threshold`
  *and* not a duplicate within `prioritization.alert_cooldown_seconds`
  (per `(symbol, alert_key)`) becomes a real `AlertGenerated` event — this
  is what "reduces notification fatigue while surfacing significant
  developments promptly" means concretely. Every decision, accepted or
  suppressed, is recorded with its reason in a bounded, queryable
  `decision_history(symbol)` — transparency without spamming the bus with
  every rejected candidate. Reads `settings.portfolio.watchlist` directly
  at construction (the same static config the Portfolio Intelligence Layer
  reads) rather than learning membership reactively from
  `SymbolProfileUpdated` sightings — that would leave a quiet watchlist
  symbol's very first legitimate alert incorrectly filtered out as "not on
  watchlist" before it ever got a profile update. No circular dependency
  with the Portfolio Intelligence Layer: each treats the other's event
  purely as cache-update input, never as a trigger to republish.
- **`SymbolProfileUpdated` / `AlertGenerated`** (`app/event_bus/events.py`)
  — two new events. `AlertGenerated` is the one event type in the whole
  platform meant to reach the user unprompted; everything else stays
  command-driven, on demand.
- **`PluginContext` gains `portfolio_engine`** (`app/plugins/base.py`,
  `app/plugins/registry.py`) — the same documented, narrow, read-only-query
  exception introduced in Milestone 5, extended for `/watchlist`'s ranked
  output and `/analyze`'s portfolio snippet.
- **`/watchlist`** (`plugins/commands/watchlist/`) — zero-parameter command
  rendering the Portfolio Intelligence Layer's `ranked_watchlist()`,
  highest priority first, with each symbol's evidence counts, matched
  strategies, active context, alert history, and full score breakdown —
  the proactive counterpart to `/analyze`'s on-demand, single-symbol view.
  Uses the same Discord Action Registry (Refresh/Dismiss) every other
  command uses.
- **`/analyze` updated** — additively shows a one-line **Watchlist
  priority** snippet (score + confidence trend + alert count) when the
  analyzed symbol happens to be on the configured watchlist; a symbol off
  the watchlist, or no `portfolio_engine` at all, degrades to exactly the
  Milestone 7 behavior.
- **Proactive Discord alert delivery** (`app/discord/bot.py`) — `TradingBot`
  subscribes to `AlertGenerated` at construction time (so an alert
  generated before the gateway connection is fully up is still queued, not
  lost) and posts a formatted message — symbol, title, score, urgency,
  transparent breakdown — to `settings.discord.alert_channel_id` (new
  `DiscordSection` field) if configured. Missing config, an uncached
  channel (falls back to `fetch_channel`), or a delivery failure are all
  logged and handled gracefully — the same non-fatal degradation pattern
  as a missing `DISCORD_BOT_TOKEN` or `ANTHROPIC_API_KEY` — never crashes
  the bot or the event bus subscriber.
- **`GET /watchlist`** — mirrors `GET /scanners`/`GET /strategies`: the
  configured watchlist plus every symbol's ranked profile.
- `config/default.yaml` gains two new tunable sections, `portfolio`
  (watchlist, confidence-trend window/margin, notable context types,
  fundamental-freshness window, alert-suppression window/factor) and
  `prioritization` (alert threshold, cooldown, watchlist-only toggle,
  decision-log size), plus `discord.alert_channel_id`.
- 63 new tests (13 Portfolio scoring math, 16 Portfolio engine, 10
  Prioritization scoring math, 15 Prioritization engine, 3 `/watchlist`
  command, 5 Discord proactive-alert-delivery, 3 `/analyze` portfolio-
  snippet integration, 3 full Milestone 8 pipeline integration incl. two
  architectural import-guardrail tests) plus existing tests updated for
  the new registered command (`/watchlist` alongside `/analyze`, `/scan`,
  `/ping`, `/help`); 325 tests passing total, ~96% coverage of `app/`
  (improved from Milestone 7's ~95%), ruff clean.
- Live-verified end to end: real indicator plugins driving a real
  "Momentum Breakout" `StrategyMatched` and a real "Bull Trend"
  `MarketContextUpdated` for one watchlisted symbol while a second
  watchlisted symbol stays quiet — the Portfolio Intelligence Layer
  produces a real non-zero, ranked-first priority score for the active
  symbol and a real (still-tracked, not dropped) zero score for the quiet
  one; the Event Prioritization Engine independently turns the same
  strategy match into a real, scored, threshold-clearing `AlertGenerated`;
  `/watchlist` and `/analyze` both reflect it — all through the real Event
  Bus, no direct calls between the two new engines (see
  `tests/test_milestone8_pipeline_integration.py`).

## Milestone 9 — Unified Simulation Engine + Decision Timeline ✅ complete

- **Clock abstraction** (`app/core/clock.py`) — `Clock` ABC (`now()`);
  `SystemClock` (real wall clock, default everywhere, so every existing
  call site is unaffected) and `SimulatedClock` (a settable virtual clock —
  `now()`, `advance_to(when)` rejecting backwards movement, `tick(delta)`).
  Injected as an optional `clock: Clock | None = None` constructor kwarg
  into every core engine whose internal math is time-sensitive:
  `EvidenceAggregator` (freshness/decay), `MarketContextEngine`,
  `StrategyEngine`, `PortfolioIntelligenceEngine` (confidence trend, alert
  suppression), `EventPrioritizationEngine` (alert cooldown). Just as
  important, every event these five engines publish
  (`EvidenceAggregated`, `MarketContextUpdated`, `StrategyMatched`,
  `SymbolProfileUpdated`, `AlertGenerated`) is now stamped with
  `timestamp=self._clock.now()` explicitly at the publish call site,
  rather than left to `Event`'s own real-wall-clock default — an engine's
  *internal* math being simulated doesn't help determinism if the *event*
  it emits still carries a real timestamp. (This exact gap — `AlertGenerated`
  still defaulting to `datetime.now()` — was caught by this milestone's own
  determinism test comparing two independent simulation runs field-for-
  field, not assumed away; see "Testing" below.) Deliberately NOT
  propagated into indicator/intelligence plugins' `Evidence.created_at` or
  `IndicatorCalculated`/`EvidenceProduced` timestamps — verified via grep
  that nothing in decision logic reads those fields, documented as a
  narrow, explicit scope boundary.
- **`EventBus.drain()`** (`app/event_bus/bus.py`) — a new public method
  waiting until every event published so far, including events published
  from *within* a handler while draining, has fully settled. Tracked with
  one bus-wide in-flight counter (incremented in `publish()` before an item
  lands in a queue, decremented after `task_done()`) rather than
  `asyncio.gather(*(q.join() for q in queues))` over each subscriber's own
  queue — the latter looks equivalent but has a real race: `Queue.join()`
  only blocks if that specific queue *already* has unfinished items at the
  moment `join()` is called, so a downstream queue that hasn't received its
  first item yet (because the handler that will publish to it hasn't run
  yet) reports "already finished" instantly, meaning `drain()` could return
  before a later hop of a multi-hop cascade even started. This is what lets
  the Simulation Engine publish one simulated bar and then deterministically
  wait for the *entire* downstream reaction to settle before advancing,
  without depending on asyncio's task-scheduling order. `shutdown(drain=True)`
  now uses this same primitive instead of duplicating the logic.
- **`FUNDAMENTAL_CATEGORIES`** (`app/evidence/schema.py`) — promoted out of
  a private duplicate in `app/portfolio/engine.py` so the Portfolio
  Intelligence Layer and the new Decision Timeline share one classification
  of which evidence categories count as "external intelligence" vs.
  technical.
- **`DecisionRecorded`** (`app/event_bus/events.py`) — a new immutable
  event: the canonical record of one point-in-time reasoning snapshot.
  Timestamp, symbol, market context, technical + fundamental evidence
  (human-readable summaries), confidence weights, matched strategies, a
  reasoning summary, `reasoning_source`, confidence, `simulated_action` (a
  `watch_bullish`/`watch_bearish`/`watch_neutral`/`no_action` hypothesis
  label — deliberately never `buy`/`sell`), the price at decision time,
  `bar_index`/`lookahead_bars`, and `outcome`/`outcome_price_change_pct`/
  `outcome_pending`.
- **Decision Timeline** (`app/timeline/`) — a new core module (not a
  plugin), the same tier as the Evidence Aggregator or Portfolio
  Intelligence Layer. `DecisionTimeline` subscribes to `DecisionRecorded`
  and maintains a bounded (`simulation.timeline_max_per_symbol`), queryable,
  per-symbol history (`for_symbol()`, `all()`, `symbols()`,
  `total_recorded`). Durable persistence needed no new database table: every
  event on the bus, `DecisionRecorded` included, is already persisted
  verbatim by the existing bus-wide `attach_event_logger` subscriber via the
  Repository pattern. `EventLogRepository.decision_records()`
  (`app/db/repository.py`) reconstructs `DecisionRecord` objects straight
  from those durable `event_log` rows.
- **Unified Simulation Engine** (`app/simulation/`, this milestone's
  centerpiece) — `SimulationEngine.run(config)` is a self-contained
  "mini-bootstrap": one isolated `EventBus`, a fresh instance of every core
  engine (all clock-injected where relevant), a fresh `PluginRegistry`
  loading `plugins/market_data` → `plugins/indicators` (+
  `plugins/intelligence` optionally) in the exact same two-phase pattern as
  `app/core/bootstrap.py`. Drives historical bars one at a time via
  `MarketDataService.fetch()` (reusing the existing deterministic
  `ReplayProviderPlugin`, never a simulation-specific data path),
  publishing `MarketDataUpdated` with an explicit simulated timestamp and
  draining the bus after each bar (and after each intelligence poll) for
  deterministic full-cascade settling before advancing. Builds
  `DecisionRecorded` snapshots every `decision_interval_bars` bars per
  symbol (buffered, never published until resolved), resolves outcomes
  once `lookahead_bars` further bars exist by comparing subsequent price
  action against the decision's implied direction (within a configurable
  neutral band), and honestly force-flushes any still-unresolved decisions
  (`outcome=None`, `outcome_pending=True`) at run end rather than
  fabricating a result. Intelligence plugins' real background polling task
  is cancelled immediately after loading (`poll_once()` itself is
  deterministic; its wall-clock-interval background loop is not
  simulation-safe) — the engine calls `poll_once()` directly on a fixed
  simulated cadence instead. The Reasoning Engine is always constructed
  with `provider=None` during simulation — no real AI provider call, for
  determinism/reproducibility/cost, using the exact same evidence-only code
  path live operation already uses whenever unconfigured. `pace: "instant"`
  (no delay, backtesting/optimization speed) vs. `"realtime"` (sleeps
  `bar_interval_seconds` between bars, a future human-watchable Replay
  Mode) is a single knob unifying "Historical Backtesting" and "Replay
  Mode" architecturally, per the spec. Stateless between calls — each
  `run()` gets its own fully isolated engines/event bus, already supporting
  "run the same historical window under different configs and compare
  results" (Strategy Comparison / Parameter Optimization) by construction.
- **`SimulationSection` config** (`app/config/settings.py`,
  `config/default.yaml`) — `default_bar_count`, `default_timeframe`,
  `bar_interval_seconds`, `pace`, `decision_interval_bars`,
  `lookahead_bars`, `outcome_neutral_band_pct`, `include_intelligence`,
  `intelligence_poll_interval_bars`, `timeline_max_per_symbol`.
  Deliberately no `seed` field — reproducibility comes from the configured
  market data provider's own already-deterministic config
  (`plugins/market_data/replay/config.yaml`), not a second seeding
  mechanism.
- 52 new tests: 9 Clock (`tests/test_clock.py`), 5 `EventBus.drain()`
  (`tests/test_event_bus.py`), 1 `FUNDAMENTAL_CATEGORIES`
  (`tests/test_evidence_schema.py`), 1 `SimulationSection` config
  (`tests/test_config.py`), 1 `EventLogRepository.decision_records()`
  (`tests/test_db.py`), 7 Decision Timeline (`tests/test_timeline.py`), 23
  Simulation Engine unit + focused-integration tests including the
  two-independent-runs determinism check (`tests/test_simulation_engine.py`),
  and 5 full Milestone 9 pipeline integration tests
  (`tests/test_milestone9_pipeline_integration.py`) — complete event
  pipeline generation, Decision Timeline reasoning/context/confidence/
  outcome recording, real-event-bus verification, `/analyze` run against a
  simulation's engines using the real unmodified `AnalyzePlugin`, and
  repeated-run isolation. Two architectural import-guardrail tests updated
  (`app.core.clock` added as a legitimate shared-utility exception for
  `MarketContextEngine` and `StrategyEngine`, alongside the Milestone 8
  updates for `PortfolioIntelligenceEngine`/`EventPrioritizationEngine`).
  377 tests passing total, ~96% coverage of `app/` (maintained from
  Milestone 8; every new module in the 97-100% range), ruff clean.
- **A real bug found and fixed by this milestone's own tests, not shipped
  around:** the first version of `EventBus.drain()` used
  `asyncio.gather(*(q.join() for q in subscribers))`, which looked correct
  but had a race — see "`EventBus.drain()`" above. A dedicated multi-hop
  cascade test (`test_drain_waits_out_a_multi_hop_cascade`) caught it
  failing intermittently; fixed with the bus-wide in-flight counter
  described above, then verified stable across repeated runs. Separately,
  the same rigor caught `AlertGenerated` (and, on inspection, four sibling
  events) publishing with real wall-clock timestamps despite every engine
  involved being fully clock-injected — see "Clock abstraction" above.
  Both fixes are load-bearing for the milestone's explicit determinism
  requirement, not cosmetic.
- Live-verified end to end: a real `SimulationEngine.run()` over 60-70
  simulated bars for NVDA/AAPL produces real `StrategyMatched`/
  `MarketContextUpdated`/`SymbolProfileUpdated`/`AlertGenerated` events
  through the real Event Bus, a populated Decision Timeline with resolved
  outcomes, and an `/analyze NVDA` response — via the real, unmodified
  `AnalyzePlugin` — that reflects the simulation's own engines exactly the
  way it would reflect live ones (see the Milestone 9 completion report for
  the transcript).

## Milestone 10 — Unified Trading Journal + Reflection Engine ✅ complete

- **`ACTION_DIRECTIONS`** (`app/event_bus/events.py`) — promoted out of a
  private `_ACTION_DIRECTIONS` duplicate in `app/simulation/engine.py` to a
  public constant (`{"watch_bullish": "bullish", "watch_bearish": "bearish",
  "watch_neutral": "neutral"}`, deliberately omitting `"no_action"`), since
  it's part of `DecisionRecorded`'s own vocabulary and is now needed by both
  the Simulation Engine (outcome resolution) and the new Reflection Engine
  (splitting evidence into supporting vs. contradictory).
- **Evidence-line text convention** (`app/evidence/formatting.py`, new
  module) — `format_evidence_line(evidence) -> str` (`"{source}: {title}
  ({direction}, {confidence:.0f}/100)"`) and its inverse,
  `parse_evidence_line(line) -> EvidenceLineParts | None` (a regex-backed
  parser that returns `None` — never raises — on anything that doesn't match
  the convention exactly). One shared implementation between the code that
  *builds* `DecisionRecorded.technical_evidence`/`fundamental_evidence`
  (the Simulation Engine) and the code that *reads* it back (the Reflection
  Engine, which only ever sees the `DecisionRecorded` event on the bus, by
  design — never a live reference to the Evidence Aggregator's original
  `Evidence` objects).
- **`ReflectionGenerated`** (`app/event_bus/events.py`) — a new immutable
  event published by the Reflection Engine, one per resolved
  `DecisionRecorded`: `symbol`, `decision_event_id` (links back to the
  triggering decision), `reasoning`, `supporting_evidence`/
  `contradictory_evidence` (evidence lines split by direction agreement),
  `market_context`, `confidence`, `confidence_evolution`
  (rising/falling/stable/unknown), `simulated_action`, `outcome`/
  `outcome_price_change_pct`, and `lessons_learned`/`potential_improvements`
  — every field the Milestone 10 spec's post-trade analysis asks for.
- **`JournalCreated`** (`app/event_bus/events.py`) — repurposed from an
  unused Milestone 1 scaffolding event (already named in `PROJECT.md`'s
  original example event list) rather than inventing a new name: `symbol`,
  `decision_event_id` (`None` is a valid, honest "general note about this
  symbol"), `trade_id` (a deliberate placeholder for a future broker
  execution system, always `None` today), `note`, `author`,
  `screenshot_url` (placeholder support only — a URL/path string, no image
  upload handling).
- **Reflection Engine** (`app/reflection/`, new core module, not a plugin)
  — `ReflectionEngine` subscribes to `DecisionRecorded` and reflects only
  when `outcome_pending is False` (the concrete interpretation of "a
  completed trade or completed simulation," since no real trade execution
  system exists yet), plus `SymbolProfileUpdated` to cache
  `confidence_trend` per symbol (the same cache-only pattern the Event
  Prioritization Engine already established — never a live call into the
  Portfolio Intelligence Layer). Generation is deterministic and rule-based
  (`_split_evidence()`, `_build_lessons()`) — never an AI call, the same
  "evidence_only"/`provider=None` default this codebase already uses
  everywhere a real model call isn't warranted. Clock-injected
  (`clock: Clock | None = None`) so a reflection's timestamp stays
  consistent with a simulated timeline — deliberately not repeating the
  wall-clock-timestamp determinism bug Milestone 9 found and fixed.
  Bounded per-symbol in-memory history
  (`reflection.history_max_per_symbol`), query surface `for_symbol()`/
  `all()`/`symbols()`/`total_generated`, `reflection.enabled` toggle for
  graceful degradation.
- **Trading Journal** (`app/journal/`, new core module, not a plugin) —
  `TradingJournal` subscribes independently to `DecisionRecorded`,
  `ReflectionGenerated`, and `JournalCreated`; it never holds a live
  reference to `DecisionTimeline` or `ReflectionEngine` objects, so "no
  subsystem communicates directly with another" holds structurally, not
  just by convention (see the import-guardrail test below).
  `JournalEntry` **wraps** (never duplicates) a `DecisionRecord` plus an
  optional `ReflectionRecord`, additive `notes`/`screenshots` (placeholder
  URLs), and `broker_execution` (always `None` today — an honest
  placeholder for the spec's "future broker execution data," never
  fabricated). `add_note()`/`add_screenshot()` only ever mutate state by
  publishing `JournalCreated`, which the engine's own `_on_journal_created`
  subscriber then reacts to — the same self-consistent event-driven pattern
  `DecisionTimeline` already uses for `DecisionRecorded`. Notes/screenshots
  without a matching `decision_event_id` go into per-symbol general-notes/
  general-screenshots buckets. Bounded via `journal.max_entries_per_symbol`/
  `journal.max_notes_per_entry`. Query surface: `get()`, `for_symbol()`,
  `all()`, `symbols()`, `general_notes_for()`, `general_screenshots_for()`,
  `total_entries`.
- **`EventLogRepository.reflections()` / `.journal_notes()`**
  (`app/db/repository.py`) — mirror `decision_records()` exactly:
  reconstruct `ReflectionRecord`/`JournalNote` objects straight from
  already-persisted `event_log` rows, Python-side symbol filtering, no
  dedicated table, no raw/dialect SQL.
- **Live + Simulation wiring** — `DecisionTimeline`, `ReflectionEngine`, and
  `TradingJournal` are now constructed and attached inside live
  `app/core/bootstrap.py` (previously, per Milestone 9,
  `DecisionTimeline` was only ever constructed inside
  `SimulationEngine.run()`) — so `/journal` and future consumers work the
  moment any producer publishes `DecisionRecorded`. Documented as sitting
  idle gracefully in live mode today, since nothing publishes
  `DecisionRecorded` from live market data yet — the same honest,
  carried-over Milestone 9 scope boundary, not a bug.
  `SimulationEngine.run()` constructs its own clock-injected
  `ReflectionEngine`/`TradingJournal` instances alongside its
  `DecisionTimeline`, attaches them to the run's isolated `EventBus`, and
  returns them on `SimulationResult`.
- **`PluginContext.trading_journal`** (`app/plugins/base.py`) — a new
  `TradingJournal | None` field, the same documented, narrow, read-only-
  query exception pattern as `portfolio_engine`/`context_engine`/etc. Its
  one "write" capability (`add_note()`) still only works by publishing an
  event the engine reacts to itself, preserving the event-only mutation
  rule.
- **`/journal SYMBOL [note]`** (`plugins/commands/journal/`) — read mode
  (no `note`) renders the Trading Journal's enriched history for a symbol:
  every entry's decision (action, confidence, outcome), its reflection
  (reasoning, supporting/contradictory evidence, confidence evolution,
  lessons learned, potential improvements), attached notes/screenshot
  counts, and any general (non-decision-specific) notes/screenshots. Write
  mode (`note` given) calls `trading_journal.add_note()` against the
  symbol's most recent entry, or as a general note if none exists yet —
  either way only ever mutating state by publishing `JournalCreated`. The
  pre-existing Action Registry "journal" button placeholder is
  deliberately left as-is: a button handler only receives
  `(interaction, target)`, no `PluginContext`, so wiring it would need a
  larger structural change this milestone's spec didn't ask for — `/journal`
  is the supported way to reach the Journal today.
- **`ReflectionSection`/`JournalSection` config**
  (`app/config/settings.py`, `config/default.yaml`) —
  `reflection.enabled`, `reflection.history_max_per_symbol`,
  `journal.max_entries_per_symbol`, `journal.max_notes_per_entry`.
- 68 new tests: 6 evidence-line formatting round-trip/malformed-input
  (`tests/test_evidence_formatting.py`), 14 Reflection Engine
  (`tests/test_reflection_engine.py` — supporting/contradictory split, all
  four `_build_lessons` branches, confidence-trend caching, bounded
  history, `enabled=False` graceful no-op, clock injection), 16 Trading
  Journal (`tests/test_trading_journal.py` — entry creation, reflection
  attachment by `decision_event_id`, matched-vs-general note/screenshot
  routing, bounded eviction, clock injection), 2 new
  `EventLogRepository.reflections()`/`.journal_notes()` durable-
  reconstruction tests (`tests/test_db.py`), and 5 full Milestone 10
  pipeline integration tests
  (`tests/test_milestone10_pipeline_integration.py`) — a completed
  simulation automatically generating enriched journal records,
  `ReflectionGenerated` actually observed flowing over the real Event Bus
  by two independent subscribers, `/journal` retrieving a complete
  historical record (read + note-write round trip) via the real,
  unmodified `JournalPlugin`, an import-guardrail test proving the
  Reflection Engine and Trading Journal never import each other's `engine`
  module or `app.timeline.engine`/`app.simulation` directly (only each
  other's plain-data `models` modules), and a two-independent-runs
  determinism check extended to reflections and journal entries. One
  pre-existing test (`test_bot_registers_help_ping_analyze_and_scan_commands`)
  updated to expect the new `journal` command alongside the existing four.
  429 tests passing total, 97% coverage of `app/` (94% on the Milestone
  10 modules specifically — `app/reflection/`, `app/journal/`,
  `app/evidence/formatting.py`, and `plugins/commands/journal/`), ruff
  clean.
- Live-verified end to end: a real `SimulationEngine.run()` over 40-60
  simulated bars for NVDA produces resolved `DecisionRecorded` events, each
  automatically followed by a `ReflectionGenerated` event over the real
  Event Bus and an enriched `JournalEntry` (decision + reflection) with no
  direct call between the two new engines; `/journal NVDA` — via the real,
  unmodified `JournalPlugin` — renders the complete history including
  reasoning, supporting/contradictory evidence, lessons learned, and
  potential improvements, and a follow-up `/journal NVDA note:"..."` note
  round-trips through `JournalCreated` and appears on the next read (see
  the Milestone 10 completion report for the transcript).

## Milestone 11 — Capital Protection Engine + Adaptive Risk Profiles ✅ complete

- **`RiskEvent`** (`app/event_bus/events.py`) — repurposed from the unused
  Milestone 1 scaffolding event `RiskWarning`, the same "reuse the intended
  vocabulary slot" decision Milestone 10 made for `JournalCreated`:
  `risk_type`, `symbol` (`None` for portfolio-wide types), `severity`
  (`info`/`warning`/`critical`), `value`, `threshold`, `applicable` (`False`
  for the honest margin/broker placeholders), `profile_name`, `message`,
  `context`. **`RISK_TYPES`** — a new public constant, the spec's 12
  canonical strings: `daily_drawdown`, `total_drawdown`,
  `trailing_drawdown`, `consecutive_losses`, `open_portfolio_risk`,
  `position_concentration`, `sector_concentration`, `symbol_concentration`,
  `correlated_exposure`, `margin_utilization`, `broker_constraints`,
  `prop_firm_compliance`.
- **`TradeOpened`/`TradeClosed`** (`app/event_bus/events.py`) — both
  pre-existing unused Milestone 1 scaffolding events, extended with
  `decision_event_id: UUID | None = None`. Synthesized by the Capital
  Protection Engine from each observed `DecisionRecorded` — never by the
  Simulation Engine directly, preserving Milestone 9's "an event is
  recorded exactly once, fully resolved" invariant. `TradeOpened` always
  publishes for a non-neutral decision; `TradeClosed` publishes immediately
  after, in the same handler call, only if the triggering decision's
  `outcome_pending is False`.
- **Capital Protection Engine** (`app/capital_protection/engine.py`, new
  core module, not a plugin) — `CapitalProtectionEngine` subscribes to
  `DecisionRecorded` and `MarketDataUpdated`. Maintains continuously
  evolving state — a running equity curve, day-boundary-aware daily
  start equity, a bounded trailing-equity window, a consecutive-losses
  streak counter, a bounded recently-closed-trades window, and a bounded
  per-symbol price history — never recomputing from scratch. Publishes a
  full round of `RiskEvent`s (all 12 `RISK_TYPES`) after every
  decision-driven evaluation cycle, using a graduated severity function
  (`ratio >= 1.0` → critical, `>= 0.7` → warning, else info) uniformly
  except for `prop_firm_compliance`'s binary pass/fail. Real Pearson
  correlation math (`_pearson`, `_returns`, `_max_correlated_pair`) over a
  bounded rolling price history, restricted to symbols with actual recent
  trading exposure, gated by a minimum sample count — verified live that
  perfectly co-moving price series produce correlation ≈1.0.
  `position_concentration`/`symbol_concentration`/`sector_concentration`
  are computed over a rolling window of recently *closed* trades (since
  concurrently open positions are rare by construction given the synthesis
  behavior above) — a distinct, real, documented risk lens. Synthetic
  position sizing (`notional = equity * profile.max_position_size_pct% *
  confidence%`) is a documented placeholder tying size to both the active
  Risk Profile's ceiling and the decision's own confidence. Margin
  utilization and broker constraints are honestly published as
  `applicable=False` — no margin/leverage or broker integration exists yet.
  The engine **never blocks a trade or a command** — only ever publishes to
  the Event Bus.
- **Adaptive Risk Profile system** (`app/capital_protection/profiles.py`,
  new module) — `RiskProfile = RiskProfileConfig` (the actual pydantic
  schema lives in `app/config/settings.py` alongside every other config
  section, per this codebase's convention). `RiskProfileRegistry` wraps
  `settings.capital_protection`: `names()`, `get()`, `active_name`,
  `current()` (graceful fallback to a safe default if the registry is ever
  empty — never raises), `set_active(name) -> bool` (logged no-op on an
  unknown name — the "profile switching without code modifications"
  mechanism), `register(profile, *, activate=False)` (adds/replaces a
  Custom Profile at runtime — the "Custom Profiles" requirement, no
  config-file-edit-plus-restart needed).
- **`CapitalProtectionSection`/`RiskProfileConfig` config**
  (`app/config/settings.py`, `config/default.yaml`) — `enabled`,
  `starting_equity`, `active_profile`, correlation/trailing/concentration
  window settings, `symbol_sectors` (symbols with no entry group under
  `"Unknown"`, never a crash), and five built-in profiles: Conservative,
  Swing Trader (the default), Day Trader, Scalper, and Prop Firm (modeled
  on real prop-firm rules — 5% max daily loss / 10% max total drawdown).
  Each configures maximum daily loss, maximum total drawdown, maximum
  position size, maximum concurrent positions, maximum portfolio exposure,
  correlation/sector/symbol limits, `max_consecutive_losses` (added beyond
  the spec's literal list, to give the required "Consecutive losses" risk
  type a configurable threshold), and `max_leverage` (always `null` today —
  an honest future-leverage placeholder, no margin system exists).
- **`EventLogRepository.risk_events()`** (`app/db/repository.py`) — mirrors
  `decision_records()`/`reflections()`/`journal_notes()` exactly:
  reconstructs `RiskEvent` objects straight from already-persisted
  `event_log` rows, with optional `symbol`/`risk_type` filtering, no
  dedicated table.
- **Live + Simulation wiring** — `app/core/bootstrap.py` constructs and
  attaches a `CapitalProtectionEngine` for live operation;
  `app/simulation/engine.py`'s `run()` constructs and attaches its own
  clock-injected instance of the exact same class, returned on
  `SimulationResult.capital_protection_engine` — literally
  "simulation and live modes using the same Capital Protection Engine."
- **Independent, direct Discord delivery** (`app/discord/bot.py`) —
  `TradingBot` gets a second, independent `RiskEvent` subscription,
  deliberately **not** routed through the Event Prioritization Engine
  (whose default `watchlist_only=True` would silently suppress every
  portfolio-wide `symbol=None` risk alert — discovered by reading
  `app/prioritization/engine.py`'s gating logic). Delivers
  `warning`/`critical` severities to `discord.alert_channel_id` with a new
  per-`(risk_type, symbol)` cooldown (`discord.risk_alert_cooldown_seconds`).
- **`PluginContext.capital_protection_engine`** (`app/plugins/base.py`) —
  the same documented, narrow, read-only-query exception pattern as
  `trading_journal`/`portfolio_engine`. Its one write-shaped exception,
  `set_active_profile()`, never blocks anything or edits a limit in code —
  only switches which already-configured profile is active.
- **`/risk [profile]`** (`plugins/commands/risk/`) — no `profile` renders
  the full `CapitalProtectionStatus` snapshot (equity, all `RISK_TYPES`
  entries including symbol-scoped concentration keys, margin/broker
  placeholders shown as "n/a", available profiles); a `profile` argument
  switches the active Risk Profile live, confirming success or listing
  available names on an unknown one — "profile switching without code
  modifications" made reachable from Discord.
- 45 new tests: 8 Adaptive Risk Profile (`tests/test_risk_profiles.py` —
  built-in profile loading, active-profile defaulting, switching,
  unknown-name no-op, Custom Profile registration/activation/replacement,
  empty-registry fallback), 23 Capital Protection Engine
  (`tests/test_capital_protection_engine.py` — continuously evolving
  equity/drawdown state, day-boundary rollover, consecutive-losses streak,
  graduated severity, all 12 `RISK_TYPES`, real Pearson correlation
  verified against perfectly co-moving price series, prop-firm binary
  compliance, position-capped sizing), 1 new
  `EventLogRepository.risk_events()` durable-reconstruction test
  (`tests/test_db.py`), 4 `/risk` command tests
  (`tests/test_risk_command.py` — graceful degradation, read mode, profile
  switch success/unknown-name), and 5 full Milestone 11 pipeline
  integration tests (`tests/test_milestone11_pipeline_integration.py`) —
  real `RiskEvent`s observed flowing over the real Event Bus during a
  simulation, live profile switching without code modification, a
  structural proof that `app.core.bootstrap` and `app.simulation.engine`
  both construct the identical `CapitalProtectionEngine` class, `/risk`
  retrieving a real status snapshot via the actual `RiskPlugin`, and an
  import-guardrail test proving the Capital Protection Engine never
  imports another core engine's `engine` module directly (and no other
  core engine imports it back). One pre-existing test
  (`test_bot_registers_help_ping_analyze_and_scan_commands`) updated to
  expect the new `risk` command alongside the existing five. A real bug
  was caught and fixed by this suite: `_daily_date` was initialized to
  `None`, so day-one's `_maybe_roll_day()` stamped "start of day" equity
  *after* that same cycle's own trade had already moved it, silently
  zeroing out day-one's real daily drawdown — fixed by initializing it
  directly from pre-trade starting equity. 455 tests passing total, 95%
  coverage of `app/` (`app/capital_protection/engine.py` at 95%,
  `profiles.py`/`models.py` at 100%), ruff clean. (One pre-existing,
  unrelated flaky test — `test_milestone8_pipeline_integration.py`'s use of
  synchronous `lambda` handlers subscribed directly to the Event Bus, which
  expects `async def` handlers — surfaces intermittently only under
  coverage instrumentation's slowdown; it passes reliably standalone and
  `app/event_bus/bus.py` has zero changes this milestone, so it's a
  pre-existing test-design issue, not a Milestone 11 regression.)
- Live-verified end to end: a real `SimulationEngine.run()` produces
  `RiskEvent`s over the real Event Bus as `DecisionRecorded` events arrive;
  `capital_protection_engine.set_active_profile("prop_firm")` immediately
  changes every subsequent evaluation's thresholds with no restart, no
  config edit, no code change; `app.core.bootstrap` and
  `app.simulation.engine` both construct
  `app.capital_protection.engine.CapitalProtectionEngine` — literally the
  same class; `/risk` — via the real, unmodified `RiskPlugin` — renders
  equity, all risk types' latest severity/value, and available profiles,
  and a follow-up `/risk profile:scalper` switches and is reflected
  immediately on the next read.

## Proposed order for what's next

These map directly to `PROJECT.md` sections. Suggested build order —
open to reordering based on what you want to see working first:

1. **A real market data provider** (Polygon/Alpaca/Finnhub/similar) —
   implements `MarketDataProviderPlugin` exactly like `ReplayProviderPlugin`
   does; add it to `settings.market_data.providers` and the Scanner Engine
   picks it up with zero changes. Needs a real API credential this
   environment doesn't have, so it's a "when you're ready" item, not a
   blocker for anything else. A real News/Earnings/Macro provider (a real
   API instead of the synthetic reference plugins) is the same story for
   `settings.intelligence`-driven plugins.
2. **AI Coach**, then **Replay Mode**, **Optimization Engine**, **Personal
   Statistics** — roughly in that order. The Trading Journal and Reflection
   Engine (Milestone 10), plus the Capital Protection Engine's `RiskEvent`
   stream (Milestone 11), already give the AI Coach its entire input
   surface (`ReflectionGenerated`'s lessons_learned/potential_improvements,
   the Journal's enriched per-symbol history, structured risk state) — the
   Coach becomes a new subscriber to events that already flow, not a new
   data pipeline. These are also what would give the Action Registry's
   Chart / News / History / Backtest / Watch / Replay / Coach actions real
   behavior instead of a placeholder reply — each is a single
   `ACTION_REGISTRY.register_handler()` call once the backing system
   exists. (`Watch` already has a real system behind it as of Milestone 8
   — `/watchlist`; `Journal` already has a real command — `/journal` — as
   of Milestone 10; `/risk` similarly as of Milestone 11 — though the
   pre-existing Action Registry *button*s still intentionally use the
   placeholder, since button handlers don't receive a `PluginContext` —
   see `plugins/commands/journal/plugin.py`'s docstring.)
3. **More External Intelligence Platform sources** — SEC filings, insider
   activity, FDA approvals, M&A, buybacks, dividends, stock splits — each
   is a new folder under `plugins/intelligence/` against the same
   `IntelligencePlugin` contract Milestone 7 established, no core changes.
4. **Real broker execution / paper trading** — the honest placeholders left
   in place across Milestones 10-11 (`JournalCreated.trade_id`,
   `JournalEntry.broker_execution`, always `None`; the Capital Protection
   Engine's `margin_utilization`/`broker_constraints` `RiskEvent`s, always
   `applicable=False`; `RiskProfileConfig.max_leverage`, always `null`)
   become real, "a completed trade" in the Reflection Engine's trigger
   condition (currently a resolved `DecisionRecorded`) can be redefined
   against an actual filled order, and the Capital Protection Engine's
   synthesized `TradeOpened`/`TradeClosed` events can instead be produced
   directly by the broker integration.

Say the word and the next milestone starts. Nothing here commits to a
specific order — just say which one you want first.
