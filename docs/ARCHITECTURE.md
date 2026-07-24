# Architecture

## Guiding rule

**Everything communicates using events. Nothing communicates directly.**
Plugins don't import each other. Plugins don't import the reasoning engine.
The reasoning engine doesn't import plugins. Every connection between
systems is a subscription to the Event Bus. This is what makes "add a
folder, get new functionality" true, and what keeps a bug in one plugin
from being able to reach into another.

```
Discord  →  Command Engine  →  Event Bus  ┬→ Plugins
                                            ├→ Evidence Aggregator → Strategy Engine
                                            ├→ Reasoning Engine
                                            └→ Database
                                                   │
                                        Discord Responses
```

Evidence specifically flows through one more hop than the diagram above
shows at a glance:

```
Indicator Plugins → EvidenceProduced → Evidence Aggregator
    → EvidenceAggregated → Strategy Engine → StrategyMatched
                         → Reasoning Engine → educational, non-directive analysis
```

Neither the Strategy Engine nor the Reasoning Engine subscribes to raw
`EvidenceProduced` — the Evidence Aggregator is the single interface both
of them consume (see below).

The Indicator Plugins in that diagram get their `MarketDataUpdated` events
from somewhere — as of Milestone 6, that "somewhere" is itself two more
decoupled hops, never a specific data provider talking directly to a
specific scanner:

```
Live Provider / Replay Engine / Historical DB / Paper Feed  (each a plugin)
    → Market Data Abstraction Layer (MarketDataService.fetch())
    → Scanner Plugin → MarketDataUpdated → Indicator Plugins → ...
```

A scanner never imports a provider, and a provider never imports a
scanner — see "Market Data Abstraction Layer" and "Scanner Engine" below.

As of Milestone 7, evidence has two more independent tributaries feeding
the same Evidence Aggregator, plus a parallel stream that shapes how
that evidence is weighted rather than adding to its count:

```
Market Data ──────────────────────────────┐
Scanner Engine → Indicator Plugins ────────┤
External Intelligence Platform ────────────┼──→ Evidence Aggregator
  (News / Earnings / Macro / future        │      → Confidence Weighting Framework
   sources — plugins/intelligence/)        │      → EvidenceAggregated
                                            │           → Strategy Engine
Market Data + Intelligence ──→ Market      │           → Reasoning Engine
  Context Engine → MarketContextUpdated ───┘                → /analyze SYMBOL
```

`External Intelligence Platform` plugins publish `EvidenceProduced` the
exact same way an indicator plugin does — the Evidence Aggregator can't
tell the two apart, by design (see "External Intelligence Platform"
below). The `Market Context Engine` is the odd one out: it never adds to
`active_evidence` itself. Its `MarketContextUpdated` events feed the
Confidence Weighting Framework's "market regime" factor and the
Reasoning Engine's synthesis — context *shapes* how evidence is read, it
isn't evidence itself (see "Market Context Engine" below).

As of Milestone 8, two more core systems sit downstream of
`EvidenceAggregated`/`StrategyMatched`/`MarketContextUpdated`, each
independently subscribed, neither calling the other directly:

```
EvidenceAggregated ────────┐
StrategyMatched ───────────┼──→ Portfolio Intelligence Layer → SymbolProfileUpdated → /watchlist, /analyze
MarketContextUpdated ──────┤
                            └──→ Event Prioritization Engine → AlertGenerated → proactive Discord alert
                                        ↑ reads cached confidence_trend from SymbolProfileUpdated (cache only, no republish)
```

The **Portfolio Intelligence Layer** continuously profiles every symbol on
the configured watchlist and ranks them by a transparent priority score —
shifting the assistant from reactive (`/analyze` on request) to proactive
(a standing, always-current picture of what deserves attention). The
**Event Prioritization Engine** independently scores the same candidate
developments for whether they're worth interrupting the user about,
publishing `AlertGenerated` only when a candidate clears a configurable
threshold and isn't a recent duplicate. Each engine consumes the other's
output purely as a cache-update input (Portfolio Intelligence Layer caches
`AlertGenerated` for historical alert state; the Prioritization Engine
caches `SymbolProfileUpdated`'s `confidence_trend`) — neither one's
handler for the other's event ever triggers a new publish, so there's no
cycle. See "Portfolio Intelligence Layer" and "Event Prioritization
Engine" below.

## Event Bus (`app/event_bus/`)

`EventBus` is an async pub/sub broker. Every subscriber gets its own bounded
`asyncio.Queue` and background worker task — so one slow or broken handler
can't block delivery to anyone else, and `publish()` naturally applies
backpressure (it awaits `queue.put`) instead of silently dropping events
under load.

- `subscribe(EventType, handler)` — deliver only events of that exact class
- `subscribe_all(handler)` — deliver every event (used by the DB event
  logger and, later, an audit/alerting plugin)
- A handler that raises is caught, logged with `event_handler_error`, and
  does not affect other subscribers
- A handler slower than `event_bus.slow_handler_threshold` (config) is
  logged with `event_handler_slow` but still allowed to finish

Events (`app/event_bus/events.py`) are immutable Pydantic models —
`MarketDataUpdated`, `PriceMoved`, `IndicatorCalculated`, `NewsReceived`,
`EarningsReleased`, `MacroEventOccurred`, `TradeOpened`, `TradeClosed`,
`PositionUpdated`, `WatchlistTriggered`, `StrategyMatched`,
`BacktestFinished`, `JournalCreated`, `DailySummary`, `RiskWarning`, plus
`EvidenceProduced` (wraps an `Evidence` object — see below),
`EvidenceAggregated` (carries `weighted_evidence`, the Confidence
Weighting Framework's output — see below), `MarketContextUpdated`
(the Market Context Engine's output — see below), and, as of Milestone 8,
`SymbolProfileUpdated` (the Portfolio Intelligence Layer's output) and
`AlertGenerated` (the Event Prioritization Engine's output — the one event
type in the platform meant to reach the user unprompted). Every event
carries `event_id`, `timestamp`, `source`, and an optional
`correlation_id` for tracing a chain of related events (e.g. a trade's
full lifecycle).

## Universal Plugin Contract (`app/plugins/`)

Every plugin subclasses `PluginBase` and implements five methods:

```python
async def initialize(self) -> None: ...   # subscribe to events, acquire resources
async def shutdown(self) -> None: ...     # unsubscribe, release resources
async def health(self) -> PluginHealth: ...
def config(self) -> dict[str, Any]: ...
def permissions(self) -> list[str]: ...   # declared intent, e.g. events.publish
```

A plugin is handed a `PluginContext` at construction: the shared
`EventBus`, the app `Settings`, and its own `plugin_config` dict (loaded
from an optional `config.yaml` next to `plugin.py`). That's the entire
surface a plugin needs — it never reaches into core modules directly.

`PluginContext` also carries `reasoning_engine`, `evidence_aggregator`,
`strategy_engine`, `market_data_service`, `plugin_registry`,
`context_engine` (Milestone 7), and (as of Milestone 8) `portfolio_engine`
— all default to `None`, and all exist for exactly one narrow, documented
reason: a plugin sometimes needs to answer an on-demand, synchronous,
read-only query instead of only reacting to events (`/analyze NVDA` needs
whatever the *current* evidence/reasoning/context/portfolio state is right
now; a scanner plugin needs the *current* bar from the Market Data
Abstraction Layer on every tick — it's the thing that starts the event
chain, not something reacting to one; `/scan`'s status report needs to see
what's currently loaded; `/watchlist` needs the *current* ranked watchlist
on demand). A plugin may read from these; it may never use them to mutate
state, publish on another system's behalf, or reach into a specific
indicator plugin's internals — the Event Bus remains the only way to make
something happen. See `PluginContext`'s docstring in `app/plugins/base.py`
and the "Discord", "Scanner Engine", "Market Context Engine", and
"Portfolio Intelligence Layer" sections below.

Note: `portfolio_engine` is the only exception queried by *two* different
core systems for two different reasons — command plugins read it
synchronously (the pattern above), while the Event Prioritization Engine
never touches it directly at all, only its `SymbolProfileUpdated` output
via the Event Bus (see "Event Prioritization Engine" below) — a plugin
reading a `PluginContext` field and a core engine reading another core
engine's events are deliberately different relationships.

**Discovery** (`app/plugins/loader.py`) walks every directory listed in
`config.plugins.search_paths`, imports each `<plugin-folder>/plugin.py`,
and picks up the one `PluginBase` subclass it defines. A folder that fails
to import, or a plugin whose `initialize()` raises, is logged and skipped —
see `app/plugins/registry.py::load_all`. **One broken plugin never takes
the rest of the app down.**

## Universal Evidence Object (`app/evidence/`)

Plugins never decide anything — they publish `Evidence`:

```json
{
  "source": "EMA",
  "category": "Trend",
  "title": "Bullish EMA Cross",
  "score": 15,
  "confidence": 91,
  "direction": "bullish",
  "metadata": { "fast": 20, "slow": 50 }
}
```

`score` is a plugin-defined weight (not bounded — comparable within a
category, not necessarily across categories). `confidence` is always 0–100.
`direction` is `bullish | bearish | neutral`, normalized case-insensitively
on input. Evidence is immutable and published as an `EvidenceProduced`
event, exactly like any other event — the Reasoning Engine subscribes to it
the same way a Discord notifier plugin would.

## Market Data Abstraction Layer (`app/marketdata/`, `plugins/market_data/`)

**The Scanner Engine never talks to Polygon, Alpaca, Finnhub, a CSV file,
or any other specific data source directly — only to `MarketDataService`.**
A market data provider (live feed, replay engine, historical database,
paper trading feed, future broker API) is a plugin, exactly like an
indicator or a Discord command: extend `MarketDataProviderPlugin`
(`app/marketdata/provider.py`, one method — `fetch(symbols, timeframe) ->
dict[str, Bar]` — on top of the Universal Plugin Contract), drop it under
`plugins/market_data/`, and it's auto-discovered the same way.

`MarketDataService` (`app/marketdata/service.py`) is built once, after
provider plugins have loaded, from `settings.market_data.providers` — a
priority-ordered list of provider names. `fetch()` asks each configured
provider in turn for whatever symbols are still missing and merges the
results, never letting a later (lower-priority) provider overwrite a
symbol an earlier one already answered. A provider that raises (a live
feed's connection drops) is logged and skipped, not fatal — the **future
multi-provider failover** PROJECT.md asks for, working today even with a
single provider configured, since adding a second is just adding another
name to the list.

**Reference provider:** `plugins/market_data/replay/` (`ReplayProviderPlugin`)
— the only provider that can be built honestly without a real market data
credential or network access. Two data sources in one plugin: if
`data_dir` is configured, `{data_dir}/{SYMBOL}.csv` is replayed bar-by-bar
(looping once exhausted — a genuine "replay engine"); any symbol without a
CSV file gets a deterministic (seeded, so reproducible run-to-run)
synthetic random walk instead, clearly fabricated data, never presented as
real. This is what lets the whole pipeline run and be demoed with zero
external setup.

## Scanner Engine (`app/scanner/`, `plugins/scanners/`)

The first continuous, always-on system in the platform. A scanner plugin
(`ScannerPlugin`, one more `PluginBase` subclass) repeatedly asks
`MarketDataService.fetch()` — never a specific provider — for the latest
bar per symbol/timeframe in its configured watchlist, and publishes
`MarketDataUpdated` for each one. **It never calls an indicator plugin
directly** — indicator plugins already discover new data by subscribing
to `MarketDataUpdated`, so a scanner's tick is indistinguishable from any
other source of that event as far as the rest of the pipeline is
concerned; this is the Event Bus's decoupling working exactly as
designed, not a special case.

`ScannerPlugin.initialize()` starts a real `asyncio` background task that
calls `scan_once()` on a loop, sleeping `interval_seconds` between ticks
(configurable per scanner). A failing tick is logged and reported via
`health()` as `degraded`, then retried on the next interval — the same
"isolate, don't crash the process" discipline every other plugin category
follows.

Concrete scanner plugins are expected to be almost entirely
configuration — watchlist, timeframes, interval — which is what makes
**"support multiple watchlists"** and **"run multiple scanners
simultaneously"** true without writing new Python: another
`plugins/scanners/<name>/config.yaml` is a second, independently-configured
scanner, with zero changes to `app/scanner/plugin.py` or to any other
scanner. `plugins/scanners/core/` (`CoreWatchlistScanner`) is the
reference, the same role `EMA`/`Ping`/`Momentum Breakout`/`ReplayProvider`
play for their respective categories.

`GET /scanners` and the `/scan` Discord command both report the same
underlying state (watchlist, timeframes, interval, health) — `/scan` reads
`context.plugin_registry` directly, the same documented `PluginContext`
read-only-query exception `/analyze` uses (see "Discord" below).

## Indicator library (`app/indicators/`, `plugins/indicators/`)

`app/indicators/` is core, not a plugin — it's shared infrastructure every
indicator plugin imports, which is what makes PROJECT.md's Indicator System
rule ("no duplicate calculations") actually true instead of aspirational:

- `bar.py` — `Bar` (one OHLCV bar), `SymbolWindow` (a bounded rolling
  per-symbol history — every plugin holds one `dict[symbol, SymbolWindow]`
  and never manages its own deque), and `bar_from_event()`, which turns a
  `MarketDataUpdated` event into a `Bar`. If the event only carries `price`
  (a raw tick), the tick becomes a degenerate bar (open == high == low ==
  close == price) — bar-based indicators still work on tick data, just with
  less intrabar range information until a real bar-aggregating feed plugin
  exists.
- `math.py` — pure, stateless calculation functions (`sma`, `ema_step`,
  `rsi`, `macd`, `atr`, `adx`, `bollinger_bands`, `donchian_channel`,
  `supertrend`, `obv`, `vwap`, `volume_profile`, `cci`, `ichimoku`). Every
  function takes plain lists and returns `None` on insufficient history
  instead of raising. Indicators needing history recompute over the whole
  retained window each update (rather than each plugin carrying its own
  continuously-compounding state) — simpler to test and verify by hand, at
  the cost of a small EMA/Wilder seed bias that's negligible once the
  window (300 bars by default) is several multiples of the period.

`plugins/indicators/` holds 14 indicator plugins (EMA from Milestone 1;
SMA, VWAP, RSI, MACD, ATR, ADX, Bollinger, Supertrend, OBV, CCI, Ichimoku,
Donchian, Volume Profile from Milestone 3), all following the same shape:
subscribe to `MarketDataUpdated`, append to a `SymbolWindow`, call into
`app.indicators.math`, publish `IndicatorCalculated` every update, and
publish `EvidenceProduced` only on an edge-triggered condition — a
threshold crossing or trend flip, never "value is currently above X",
which would otherwise spam fresh evidence on every single tick a symbol
spends in an extreme state. See `docs/PLUGIN_GUIDE.md` for how to add
another one.

## External Intelligence Platform (`app/intelligence/`, `plugins/intelligence/`)

PROJECT.md's Milestone 7 spec is explicit: **no separate isolated News,
Earnings, Macro, SEC Filings, Insider Activity, or Economic Calendar
engines.** Every non-price source of market information is just another
plugin producing the same two things every evidence producer in this
codebase produces — a normalized **Intelligence Event** (a typed fact:
`NewsReceived`, `EarningsReleased`, `MacroEventOccurred`, ...) and a
normalized **Evidence Object**, published exactly like an indicator
plugin's. The Evidence Aggregator doesn't know or care whether a piece of
evidence came from an RSI cross or a positive earnings surprise.

`IntelligencePlugin` (`app/intelligence/plugin.py`) is the one shared
piece of infrastructure: a config-driven polling loop (`interval_seconds`,
mirroring `ScannerPlugin`'s tick loop — most real intelligence sources are
polled on an interval in practice) and a `_publish(event, evidence)`
helper that keeps the pair from drifting out of sync. A concrete plugin
overrides one method, `poll_once()`.

**Reference plugins** (`plugins/intelligence/news/`, `earnings/`, `macro/`)
— three independent examples, each its own file, sharing only the base
contract. Like `ReplayProviderPlugin` (Milestone 6), none of them call a
real external API (no network access is assumed available in this
sandbox): they generate plausible, clearly-labeled synthetic data
(`provider: "synthetic-news-feed"`, etc.), deterministically seeded per
symbol so output is reproducible. Swapping in a real provider (a news
API, an earnings calendar service, an economic-release feed) is a new
plugin against the exact same contract — zero changes anywhere else.
Adding a *new* source category (SEC filings, insider transactions, FDA
approvals, buybacks, ...) is the same: a new folder under
`plugins/intelligence/`, not a new subsystem.

The Macro plugin also demonstrates the convention the Market Context
Engine reads to promote raw intelligence into higher-level context: any
evidence whose `metadata` carries a `context_hint` key (e.g.
`"fed_week"`, `"cpi_day"`, `"earnings_season"`) becomes a
`MarketContextUpdated` event without the Context Engine needing to know
anything about which plugin published it.

## Market Context Engine (`app/context/`)

The rest of the platform reasons about individual pieces of evidence
("Bullish EMA Cross"). This engine reasons about the *environment* those
pieces of evidence are appearing in — Bull/Bear Trend, Sideways Market,
High/Low Volatility, Gap Day, Trend Exhaustion, Low Liquidity, market-wide
Risk-On/Risk-Off, and calendar/macro context (Fed Week, CPI Day, Earnings
Season, ...). Every derivation is a real, computed signal, not a
hardcoded label:

- **Trend, volatility, gap, exhaustion, liquidity** — computed per symbol
  from a bounded rolling window of `MarketDataUpdated` closes/volumes the
  engine keeps itself (it never calls the Scanner Engine or an indicator
  plugin). Trend is a % change over `context.trend_window` bars against
  configurable thresholds; volatility is the standard deviation of
  bar-over-bar returns; a gap is a single large jump between consecutive
  updates; exhaustion is a decelerating second half of the trend window
  relative to the first; liquidity compares the latest volume against the
  trailing average.
- **Market-wide Risk-On/Risk-Off** — a genuine cross-symbol aggregate:
  once enough symbols are tracked, a majority in Bull Trend publishes
  Risk-On, a majority in Bear Trend publishes Risk-Off.
- **Macro/calendar context** — promoted from intelligence evidence
  carrying `metadata["context_hint"]` (see "External Intelligence
  Platform" above) — a generic mechanism, not hardcoded per source.

Every label lives at a `(symbol, context_type)` key, `symbol=None` for
market-wide context. Publishing is **edge-triggered** — a
`MarketContextUpdated` event fires only when a label actually changes,
same "don't spam the bus" discipline as `StrategyMatched` and the Scanner
Engine. `MarketContextEngine.snapshot(symbol)` answers the same question
on demand, the same pattern `EvidenceAggregator.snapshot()` and
`ReasoningEngine.evidence_for()` already use.

The engine never calls the Evidence Aggregator, Strategy Engine, or
Reasoning Engine directly — only `MarketContextUpdated` leaves this
module, and only through the Event Bus (checked structurally in
`tests/test_milestone7_pipeline_integration.py`, the same guarantee the
Strategy and Scanner Engines already have).

## Evidence Aggregator (`app/aggregation/`)

Sits between every evidence producer (14 indicator plugins + the
News/Earnings/Macro intelligence plugins today; more External
Intelligence Platform sources later) and everything that consumes
evidence. It is the single interface both the Strategy Engine and the
Reasoning Engine subscribe to — neither one ever subscribes to raw
`EvidenceProduced` directly. Its job is explicitly **not** to suppress or
discard market information; every `EvidenceProduced` event it ever receives
is retained in a bounded per-symbol history (`EvidenceAggregator.history()`).
What it adds on top of the raw stream:

- **Deduplication** — repeated confirmations of the exact same finding
  (same `source` + `title`) collapse to one representative in the "active"
  snapshot, while the repeat count is preserved as enrichment metadata
  (`occurrence_count`) rather than thrown away.
- **Freshness / decay** — each piece of evidence has a freshness that
  decays linearly to zero over `aggregation.freshness_window_seconds`
  (900s / 15 minutes by default). Only fresh evidence appears in the
  active snapshot; stale evidence ages out automatically instead of
  accumulating forever.
- **Conflict detection** — if the currently-fresh evidence for a symbol
  contains both bullish and bearish directions, the snapshot is flagged
  `has_conflict=True` rather than silently averaging them away.
- **Confidence weighting** (Milestone 7) — every active piece of evidence
  also gets a normalized `[0, 1]` weight from the Confidence Weighting
  Framework (below), computed alongside — never instead of — the raw,
  unweighted evidence.

Every incoming `EvidenceProduced` results in exactly one `EvidenceAggregated`
event, carrying the original evidence, its enrichment metadata, the
resulting deduped/fresh snapshot (`active_evidence`) for that symbol, and
its confidence-weighted counterpart (`weighted_evidence`).
`EvidenceAggregator.snapshot(symbol)` computes the same thing on demand,
without waiting for the next event.

## Confidence Weighting Framework (`app/aggregation/weighting.py`)

Extends the Evidence Aggregator from "how many pieces of evidence exist"
to "how much should each piece actually count." `compute_weight()`
produces a normalized `[0, 1]` weight plus a fully transparent
`breakdown` dict for every active piece of evidence, considering:

| Factor | What it reads |
| --- | --- |
| Source / historical reliability | `confidence_weighting.source_reliability` config (per-source multiplier; also stands in for historical reliability until a real trade-outcome history exists) |
| Freshness | The aggregator's own `EnrichmentInfo.freshness` |
| Persistence | `EnrichmentInfo.occurrence_count`, diminishing returns |
| Timeframe alignment | How many other active peers share this evidence's timeframe |
| Cross-indicator confirmation | How many other active peers agree in direction |
| Contradictory evidence | A penalty when active peers take the opposite directional stance |
| Market regime | Whether this evidence's direction agrees with the Market Context Engine's current trend label for the symbol |
| Correlation between sources | A documented proxy — 1/√n dampening for evidence sharing a category — *not* real statistical correlation |
| Future ML adjustments | An explicit no-op seam (`ml_adjustment`, always `1.0` today) |

Every factor multiplies around a neutral baseline; the product is clamped
to `[0, 1]`. **The original Evidence objects are never modified, replaced,
or discarded** — `weighted_evidence` is always a parallel, explainable
annotation alongside `active_evidence`, matching PROJECT.md's explicit
requirement that the framework "enhance reasoning, never replace the
underlying evidence." The Evidence Aggregator subscribes to
`MarketContextUpdated` purely as a weighting input (the "market regime"
factor) — it's never added to `active_evidence` itself.

## Portfolio Intelligence Layer (`app/portfolio/`)

Not a plugin — a core system, the same tier as the Evidence Aggregator or
Market Context Engine. Continuously monitors every symbol in
`settings.portfolio.watchlist` (config-driven membership — a symbol
outside it is never profiled, so "watch a new symbol" is a config change,
never a code change) and maintains a `SymbolProfile` per symbol,
synthesizing:

- **Current technical evidence** — active/bullish/bearish/neutral counts
  and the Confidence Weighting Framework's `top_weight`/`avg_weight`,
  reused directly from `EvidenceAggregated.weighted_evidence`, never
  recomputed.
- **External intelligence freshness** — whether News/Earnings/Macro
  evidence has arrived within `portfolio.fundamental_freshness_seconds`.
- **Market context** — the Market Context Engine's current labels for the
  symbol, read purely via `MarketContextUpdated`.
- **Confidence trend** — rising/falling/stable/unknown, from comparing the
  older half against the recent half of a rolling window
  (`portfolio.confidence_trend_window`) of average evidence weight.
- **Strategy matches** — the most recent `StrategyMatched` names, bounded.
- **Historical alert state** — `last_alert_at`/`alert_count`, cached
  purely from observing the Event Prioritization Engine's `AlertGenerated`
  output (never by deciding whether to alert itself — that stays the
  Prioritization Engine's job, one layer downstream).

`app/portfolio/scoring.py::compute_priority()` turns all of that into a
transparent `[0, 100]` `priority_score` + `breakdown` dict — evidence
strength, fundamental freshness, context intensity (capped so many labels
don't dominate on count alone), confidence trend, a flat strategy-match
bonus, and an alert-suppression *dampening* factor (configurable, default
0.5×, never a hard zero) for a symbol alerted on within
`portfolio.alert_suppression_seconds` — so the watchlist doesn't keep
re-surfacing the same just-alerted development at the top, without hiding
it either. Publishes `SymbolProfileUpdated`, edge-triggered on a
meaningful (≥0.5) score change — the same "don't spam the bus" discipline
every other engine in this codebase follows. `snapshot(symbol)` and
`ranked_watchlist()` are read-only, deep-copy query methods — `/watchlist`
and `/analyze`'s portfolio snippet both call them on demand rather than
maintaining their own state.

Never calls the Evidence Aggregator, Strategy Engine, Reasoning Engine, or
Event Prioritization Engine directly — only `SymbolProfileUpdated` leaves
this module, and only through the Event Bus (checked structurally in
`tests/test_milestone8_pipeline_integration.py`).

## Event Prioritization Engine (`app/prioritization/`)

Also not a plugin — a core system sitting between the Evidence Aggregator
(plus the Strategy Engine and Market Context Engine) and user
notifications. Every candidate development it sees —
`EvidenceAggregated`, `StrategyMatched`, `MarketContextUpdated` — is
scored by `app/prioritization/scoring.py::compute_alert_score()`:

| Factor | What it reads |
| --- | --- |
| Importance | Source-specific: a strategy match starts from a high flat base; a context shift's base depends on whether its `context_type` is inherently high-stakes (Gap Day, Risk-Regime, macro events) vs. routine (trend/volatility drift); raw evidence's base scales with the Confidence Weighting Framework's own weight for that item |
| Novelty | `1 / occurrence_count` for evidence (a first sighting counts fully, a fifth repeat a fifth as much); always `1.0` for strategy/context candidates, since those are already edge-triggered one layer upstream |
| Confidence change | A bonus when the Portfolio Intelligence Layer's cached `confidence_trend` for the symbol is rising or falling — "stable" contributes nothing, since nothing changed |
| Urgency | A source-specific `[0, 1]` time-sensitivity signal (Gap Day / macro events score high; routine trend continuation scores low; raw evidence uses a documented magnitude proxy on the plugin's own `score`) |
| User relevance | A flat bonus when the symbol is on the configured watchlist |

Only a candidate whose total score clears `prioritization.alert_threshold`
**and** isn't a duplicate within `prioritization.alert_cooldown_seconds`
(tracked per `(symbol, alert_key)`) becomes a real `AlertGenerated` event
— this is what "reduce notification fatigue while surfacing significant
developments promptly" means concretely, not just a slogan. Every
decision — accepted or suppressed, and why — is recorded in a bounded,
queryable `decision_history(symbol)`, so the logic stays transparent
without publishing every rejected candidate onto the bus.

By default (`prioritization.watchlist_only: true`) only watchlist symbols
are eligible for alerts at all. Watchlist membership is read directly from
`settings.portfolio.watchlist` **at construction**, the same static config
the Portfolio Intelligence Layer reads — deliberately not learned
reactively from `SymbolProfileUpdated` sightings, which would leave a
quiet watchlist symbol's very first legitimate alert candidate incorrectly
filtered out as "not on watchlist" before it ever produced a profile
update. The engine does subscribe to `SymbolProfileUpdated`, but purely to
cache `confidence_trend` per symbol — a genuine cross-engine runtime
signal, unlike watchlist membership.

Never calls the Portfolio Intelligence Layer, Evidence Aggregator,
Strategy Engine, or Reasoning Engine directly — only `AlertGenerated`
leaves this module, and only through the Event Bus (checked structurally
in `tests/test_milestone8_pipeline_integration.py`).

## Strategy Engine (`app/strategy/`, `plugins/strategies/`)

A strategy is **pure declarative YAML**, never Python — `plugins/strategies/
<name>/strategy.yaml`, parsed into a `StrategyDefinition` and compiled once
(not re-parsed on every evaluation) into an immutable `CompiledStrategy`:
`required`/`optional` evidence titles become frozensets (O(1) membership
checks), and evaluation is a handful of set operations plus a score sum —
the "rule graph" PROJECT.md asks for, built once at load time.

**The Strategy Engine knows nothing about EMA, RSI, MACD, or any other
specific indicator.** `app/strategy/compiler.py` and `app/strategy/engine.py`
only ever read `Evidence.title`, `Evidence.source`, `Evidence.score`,
`Evidence.direction`, and `Evidence.metadata` — the same vocabulary any
future evidence producer already speaks. Dropping in a 15th indicator
plugin makes its evidence titles usable by any strategy's `required`/
`optional` lists with zero changes to this module.

A strategy matches when every `required` evidence title is present (fresh,
per the aggregator) **and** the summed score of present required +
optional evidence reaches `minimum_score`. `StrategyEngine` subscribes to
`EvidenceAggregated`, re-evaluates every compiled strategy per symbol on
each update, and publishes `StrategyMatched` only on the transition from
not-matched to matched — edge-triggered, the same "don't spam on every tick
a condition continues to hold" rule every indicator plugin already follows.

**Repeat-policy filtering.** Some evidence (Donchian breakouts, for
instance) can legitimately fire on every single bar of a sustained trend —
mathematically correct, not a bug (see the Indicator library section
below). A strategy's `repeat_policy` maps an evidence *source* to
`every_breakout` (default — accept every occurrence), `first_breakout`
(only the first occurrence in its current sequence), or `after_pullback`
(like `first_breakout`, but additionally excludes a cold-start first
occurrence that has no real prior sequence to have pulled back from). This
filter is generic and metadata-driven (`app/strategy/compiler.py::
_passes_repeat_policy`) — it reads `metadata["is_first_in_sequence"]` /
`metadata["is_first_ever"]`, a documented convention any evidence producer
can opt into, not a Donchian-specific special case. Evidence that doesn't
carry this metadata always passes (fails open) regardless of policy.

The reference strategy, `plugins/strategies/momentum_breakout/`, is to the
Strategy Engine what `EMA` is to indicators and `Ping` is to Discord
commands — a real, working example new strategies can be modeled on. See
`docs/PLUGIN_GUIDE.md` for the authoring guide.

## Reasoning Engine (`app/reasoning/`)

Subscribes to `EvidenceAggregated` (never raw `EvidenceProduced` — see
Evidence Aggregator above), `StrategyMatched`, and (Milestone 7)
`MarketContextUpdated`. On every `EvidenceAggregated` update it replaces
its per-symbol evidence view with the aggregator's current deduped/fresh
`active_evidence` *and* the Confidence Weighting Framework's
`weighted_evidence` — freshness/decay/weighting is the aggregator's job,
so this engine always reasons over exactly "what's true right now, and
how much it should count," not an ever-growing pile of stale, unweighted
history. On `analyze(symbol)` it synthesizes everything currently
gathered (evidence, declaratively-matched strategies, and current market
context — both symbol-specific and market-wide) into a `ReasoningOutput`:
market summary, trade thesis (framed as a hypothesis, never a directive),
risk assessment, alternative scenario, confidence, suggested strategy
archetypes (populated from real `StrategyMatched` events when there are
any), historical similarity, and the context labels actually used
(`ReasoningOutput.context`).

In AI mode, each evidence item sent to the model carries its
`confidence_weight` alongside the plugin's own `confidence`, and the
current market context is appended to the prompt as its own section — the
model reasons with weighting and regime information, not just a flat list
of evidence. In evidence-only mode, when weighted evidence is available
the bullish/bearish lean and the reported confidence are computed from
weighted mass (`Σ weight` per direction) rather than raw counts, so a
handful of highly-weighted, regime-aligned signals can outweigh a larger
pile of low-weight noise — and the summary text names the current context
labels directly.

Three states, always explained rather than silent:

1. **`insufficient_evidence`** — fewer than `reasoning.min_evidence_count`
   pieces of evidence exist for the symbol yet.
2. **`evidence_only`** — no AI provider configured (no `ANTHROPIC_API_KEY`,
   or `reasoning.enabled: false`). A deterministic summary is built
   directly from the evidence (direction tally, weighted average
   confidence) and clearly labeled as evidence-only.
3. **`ai`** — a provider is configured. The engine sends the accumulated
   evidence to Claude with a system prompt that explicitly states the
   assistant is not a signal-selling bot and must cite its evidence. If the
   call fails or returns unparseable output, the engine logs the failure
   and falls back to `evidence_only` rather than crashing or going silent.

Swapping providers means implementing `ReasoningProvider.generate()` — the
engine itself never imports the Anthropic SDK directly.

## Database (`app/db/`)

PostgreSQL via async SQLAlchemy 2.0, migrations via Alembic (async
`env.py`, driven by `app.config.get_settings()` — the DB URL is never
duplicated into `alembic.ini`), and a generic `Repository[ModelT]` — no raw
SQL anywhere in the codebase.

Milestone 1 ships one table, `event_log`, and `attach_event_logger()` wires
a `subscribe_all` handler that persists every event published on the bus.
This is what makes "everything logged" true at the storage layer — future
milestones add domain tables (trades, journals, watchlists, strategies) the
same way, without touching this layer.

## Discord (`app/discord/`)

The bot is the "Command Engine" in the architecture diagram, and it's kept
deliberately thin. A Discord slash command is itself a plugin — drop a
folder under `plugins/commands/` implementing `DiscordCommandPlugin`
(adds one method, `execute()`, on top of the Universal Plugin Contract) and
it's auto-discovered and registered the same way an indicator or scanner
plugin is. No command is hardcoded into the bot except the built-in
`/help`, which has to know about every other command to list them.

The logic worth testing is split from the part that genuinely needs a live
Discord connection:

- **`dispatch.py`** — framework-agnostic. `dispatch_command(plugin,
  event_bus, ctx)` publishes a `CommandInvoked` audit event (this is what
  makes "everything logged" true for commands), runs the plugin's
  `execute()`, and if it raises, publishes `CommandFailed`, logs the
  exception, and returns a graceful error response — a broken command
  never crashes the bot process, same isolation policy as plugin loading.
  `CommandContext` and `CommandResponse` are plain dataclasses, not
  discord.py objects, so this is fully unit-testable without a gateway
  connection.
- **`bot.py`** — the thin adapter. `TradingBot` wraps `discord.Client` +
  `app_commands.CommandTree`. Its `setup_hook()` (called by discord.py once,
  before it opens the gateway) calls `register_command_plugins()`, which
  finds every loaded `DiscordCommandPlugin` and wires a small callback that
  extracts fields off the real `discord.Interaction` and calls
  `dispatch_command`. Command name collisions and invalid names are logged
  and skipped, not fatal. Slash commands sync instantly to
  `DISCORD_GUILD_ID` if set (fast, for development); otherwise they sync
  globally (can take up to an hour to propagate — normal Discord behavior).

**Parameterized commands.** A command plugin declares its slash-command
parameters as data — `DiscordCommandPlugin.parameters`, a tuple of
`CommandOption(name, description, required)` — never as a hand-written
discord.py callback. discord.py derives a command's options by inspecting
the callback function's Python signature, so there's no supported way to
attach options to a command without a matching signature; since the
options are plugin-declared data, `bot.py`'s
`_build_parameterized_callback()` builds that function at registration
time (`exec`, deliberately, with a docstring explaining why). Every
declared option is currently string-typed — see `CommandOption`'s
docstring before adding int/float/bool/choice support. A command with no
parameters (`/ping`, `/help`) skips this entirely, same as before.

**Interactive buttons.** `CommandResponse` can carry `buttons: list[
CommandButton]` — plain dataclasses (`label`, `custom_id`, `style`), never
real discord.py components, so a command plugin declaring buttons stays
testable without discord.py. `bot.py` turns these into a real
`discord.ui.View` when sending the response.

**Discord Action Registry (`app/discord/actions.py`).** Milestone 5 had
each command build its own `CommandButton`s and left `bot.py` owning the
one-size-fits-all click behavior. Milestone 6 centralizes both: a command
plugin declares which reusable *actions* it wants —

```python
ACTION_REGISTRY.buttons_for(["chart", "news", "watch", "dismiss"], target=symbol)
```

— instead of constructing buttons or implementing click behavior. The
registry owns button creation (consistent label/style per action key),
callback registration, placeholder behavior (any action without a real
handler registered gets a generic, honest "not built yet" reply), and a
documented (currently no-op — no role/permission system exists yet)
permission-check seam. `custom_id` convention is `"{action_key}:{target}"`
— action-first and command-agnostic, so the same button behaves
identically no matter which command attached it. `"dismiss"` is the one
action with a real handler today (deletes the message); giving
Chart/News/History/Backtest/Journal/Watch/Refresh/Replay/Coach real
behavior later is `ACTION_REGISTRY.register_handler(key, handler)` once,
here — every command already asking for that action key picks it up
automatically, with zero command-plugin changes.

**Reference plugins:** `plugins/commands/analyze/` (`/analyze SYMBOL`) —
one required `symbol` option, seven actions (Chart / News / History /
Backtest / Journal / Watch / Dismiss). Reads `context.evidence_aggregator`,
`context.reasoning_engine`, `context.context_engine` (Milestone 7), and
(Milestone 8) `context.portfolio_engine` directly (the documented
`PluginContext` exception above) to answer the query synchronously, and
gracefully reports "insufficient evidence" for any symbol nothing has
published `MarketDataUpdated` for yet. Its rendered output demonstrates
technical + fundamental evidence counts, a **Market context** line built
from `context_engine.snapshot()` (market-wide context first, symbol-
specific winning on any collision), the top confidence-weighted evidence
from `snapshot.weighted_evidence`, and — additively, only when the symbol
is on the configured watchlist — a **Watchlist priority** line from
`portfolio_engine.snapshot(symbol)`. `plugins/commands/scan/` (`/scan`) —
zero parameters, reports what the Scanner Engine is currently watching via
`context.plugin_registry`, using the same Action Registry
(Refresh / Dismiss) — proof the registry is genuinely reusable across
commands, not `/analyze`-specific. `plugins/commands/watchlist/`
(`/watchlist`, Milestone 8) — zero parameters, renders
`portfolio_engine.ranked_watchlist()`: every configured symbol, highest
priority first, with its evidence counts, matched strategies, active
context, alert history, and full score breakdown — the proactive
counterpart to `/analyze`'s on-demand, single-symbol view. Same Action
Registry (Refresh / Dismiss) as `/scan`.

**Proactive alert delivery (Milestone 8).** `AlertGenerated` is the one
event type in the platform meant to reach the user unprompted — everything
else above is command-driven, on demand. `TradingBot` subscribes to it at
construction time (before the gateway connection is even open, so an early
alert is queued, not lost) and posts a formatted message (symbol, title,
score, urgency, transparent breakdown) to `settings.discord.alert_channel_id`
if configured. Missing configuration, an uncached channel (falls back to
`fetch_channel`), or a send failure are all logged and handled gracefully —
the same non-fatal degradation pattern as a missing `DISCORD_BOT_TOKEN` —
never crashes the bot or the event bus subscriber that delivered it.

**What can and can't be verified without a live Discord connection:** the
whole pipeline up to and including "does this Interaction produce the right
`send_message` call" (or, for alerts, the right `channel.send` call) is
unit tested with a duck-typed fake `Interaction`/channel (see
`tests/test_discord_bot.py`). Actually opening the gateway connection
(`bot.start(token)`) can only be exercised against Discord's real servers —
that happens when you run `docker compose up` on your own machine with a
real `DISCORD_BOT_TOKEN` set. See `docs/DISCORD_BOT_SETUP.md`.

## Core / lifecycle (`app/core/`)

`bootstrap()` brings systems up in dependency order (logging → event bus →
database → Market Context Engine → Evidence Aggregator → Strategy Engine →
Portfolio Intelligence Layer → Event Prioritization Engine → Reasoning
Engine → plugin registry) and `teardown()` reverses it. This ordering is
also just bootstrap reading top-to-bottom the same way data actually
flows — every one of these attaches purely via event-bus subscriptions, so
the actual order doesn't functionally matter to any of them. Plugin
loading is deliberately two phases, not one:

1. **Phase 1** — `plugin_registry.load_all(root, search_paths=["plugins/market_data"])`
   loads only market data provider plugins. `MarketDataService` is then
   built from the result (it needs concrete provider instances to exist)
   and handed to the registry via `set_market_data_service()`.
2. **Phase 2** — every remaining search path (indicators, commands,
   scanners, strategies-adjacent categories, ...) loads normally, now with
   a real `MarketDataService` available in every `PluginContext` — this is
   what a scanner plugin's `initialize()` needs before it can start
   ticking.

If `DISCORD_BOT_TOKEN` isn't set, the bot is skipped entirely and a
warning is logged — the same graceful-degradation pattern used when no
`ANTHROPIC_API_KEY` is set for the Reasoning Engine, or when no market
data provider is discoverable for `MarketDataService`. `create_app()`
wires bootstrap/teardown into a FastAPI ASGI
[`lifespan`](https://fastapi.tiangolo.com/advanced/events/), which is also
how **graceful shutdown** works: uvicorn intercepts SIGINT/SIGTERM, runs
the lifespan shutdown phase (closing the Discord bot first, then plugins
— which cancels every scanner's background tick loop — then the event
bus, then the database), and only then exits — so `docker compose stop`
always tears everything down cleanly before the container exits.

- `GET /health` — overall status, DB reachability, Discord connection
  state (`not_configured` / `connecting` / `connected`), per-plugin health
- `GET /plugins` — loaded plugin metadata + any that failed to load
- `GET /strategies` — loaded strategy definitions (required/optional
  evidence, minimum score, repeat policy)
- `GET /scanners` — loaded scanner plugins (watchlist, timeframes,
  interval, health) and the currently configured market data provider(s)
- `GET /watchlist` — the configured watchlist plus every symbol's current
  ranked `SymbolProfile` from the Portfolio Intelligence Layer

## Configuration (`app/config/`)

`config/default.yaml` holds every non-secret behavior. `.env` /
environment variables hold secrets and per-deployment overrides, and always
win. Nothing is hardcoded — see `app/config/settings.py` for the full,
typed schema (`Settings`, cached via `get_settings()`).
