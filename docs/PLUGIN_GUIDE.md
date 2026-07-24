# Writing a plugin

No plugin ever modifies core code. Adding a folder is the whole
integration surface. This guide builds a tiny indicator plugin from
scratch — the same pattern applies to strategies, scanners, news sources,
broker integrations, anything under `/plugins`.

## 1. Pick a category folder and create your plugin's folder

```
plugins/indicators/rsi/
    plugin.py
    config.yaml     # optional
```

The category (`indicators`) must be one of `config.plugins.search_paths` in
`config/default.yaml`. The plugin folder name (`rsi`) is just for humans —
what matters is the `name` attribute on your class.

## 2. Implement the contract

```python
# plugins/indicators/rsi/plugin.py
from typing import Any

from app.event_bus import EvidenceProduced, IndicatorCalculated, MarketDataUpdated
from app.evidence import Evidence, EvidenceCategory
from app.plugins.base import PluginBase, PluginHealth, PluginPermission


class RSIPlugin(PluginBase):
    """14-period RSI with overbought/oversold evidence."""

    name = "RSI"
    version = "0.1.0"
    category = "indicators"

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._period = int(context.plugin_config.get("period", 14))
        self._subscription = None

    async def initialize(self) -> None:
        self._subscription = self.context.event_bus.subscribe(MarketDataUpdated, self._on_market_data)

    async def shutdown(self) -> None:
        if self._subscription:
            self._subscription.unsubscribe()

    async def health(self) -> PluginHealth:
        return PluginHealth(status="healthy")

    def config(self) -> dict:
        return {"period": self._period}

    def permissions(self) -> list[str]:
        return [PluginPermission.EVENTS_SUBSCRIBE, PluginPermission.EVENTS_PUBLISH]

    async def _on_market_data(self, event: MarketDataUpdated) -> None:
        rsi_value = ...  # your calculation
        await self.context.event_bus.publish(
            IndicatorCalculated(source=self.name, symbol=event.symbol, indicator="RSI", value=rsi_value)
        )
        if rsi_value > 70:
            await self.context.event_bus.publish(
                EvidenceProduced(
                    source=self.name,
                    evidence=Evidence(
                        source=self.name,
                        category=EvidenceCategory.MOMENTUM,
                        title="RSI Overbought",
                        score=10,
                        confidence=70,
                        direction="bearish",
                        symbol=event.symbol,
                        metadata={"rsi": rsi_value, "period": self._period},
                    ),
                )
            )
```

Rules the loader enforces:

- Exactly one `PluginBase` subclass per `plugin.py`
- `name` should be unique across all plugins (a collision is logged and the
  second one is skipped)
- All five contract methods are required — the loader will refuse to
  instantiate a class missing any of them (Python's `abstractmethod` won't
  let you construct it)

## 3. Optional: `config.yaml`

```yaml
# plugins/indicators/rsi/config.yaml
period: 14
```

Loaded automatically into `context.plugin_config`. This is what
"Configuration over Code" means at the plugin level — never hardcode a
tunable value in `plugin.py`.

## 3.5. Use `app.indicators`, don't recalculate

`PROJECT.md`'s Indicator System explicitly says "no duplicate
calculations" — if you're writing a new indicator plugin, the RSI example
above is illustrative, but the calculation itself (`rsi_value = ...`)
should never be hand-rolled in `plugin.py`. Use the shared library instead:

- `app.indicators.bar.SymbolWindow` — a bounded per-symbol rolling history
  of bars (default 300). Call `.append(bar_from_event(event))` on every
  `MarketDataUpdated`, then read `.closes` / `.highs` / `.lows` /
  `.volumes`.
- `app.indicators.math` — pure calculation functions (`sma`, `ema_step`,
  `rsi`, `macd`, `atr`, `adx`, `bollinger_bands`, `donchian_channel`,
  `supertrend`, `obv`, `vwap`, `volume_profile`, `cci`, `ichimoku`). Every
  one returns `None` when there isn't enough history yet instead of
  raising — check for that before publishing evidence, the same way
  `EMAPlugin` checks `state.updates < 2`.

See `plugins/indicators/rsi/plugin.py` (and any of its 12 siblings added in
Milestone 3) for the real, current version of the pattern sketched above —
in particular, note that it fires evidence only on the RSI *crossing* the
70/30 threshold (edge-triggered), not on every update where RSI happens to
already be above 70, so a symbol sitting in overbought territory doesn't
spam a fresh piece of evidence on every tick.

## 4. What you get for free

- **Auto-discovery** — drop the folder in, restart the app, it's live. No
  registration step anywhere else in the codebase.
- **Isolation** — if `initialize()` raises, your plugin is excluded and
  logged (`registry.failed["RSI"]`); nothing else breaks.
- **Health reporting** — `GET /plugins` and `GET /health` surface whatever
  your `health()` returns.
- **Everything logged** — every event you publish is automatically
  persisted to `event_log` by the database's event logger; you don't need
  to log evidence yourself.

## 5. What you must never do

- Never import another plugin's module directly. If you need something
  another plugin knows, subscribe to the event it publishes.
- Never import `app.db` and write your own queries from inside a plugin
  in this milestone's pattern — future milestones may expose a
  plugin-scoped repository; for now, evidence and events are the interface.
- Never return a trade decision. Evidence only — `direction` +
  `confidence` + `metadata` is the full vocabulary. The Reasoning Engine
  decides what evidence means; a plugin never does.

## 6. Test it

```python
# tests/test_rsi_plugin.py
async def test_rsi_publishes_evidence_on_overbought(event_bus, settings):
    registry = PluginRegistry(event_bus, settings)
    await registry.load_all(PROJECT_ROOT)
    assert "RSI" in registry.plugins
    # feed MarketDataUpdated events, assert on published EvidenceProduced
```

See `tests/test_ema_plugin.py` for a complete worked example against the
reference `EMA` plugin.

---

# Writing a Discord command plugin

A `/slash-command` is a plugin too — the same auto-discovery, the same
isolation, the same "drop a folder in" rule. The only difference is which
base class you extend and which folder you use.

## 1. Create the folder under `plugins/commands/`

```
plugins/commands/echo/
    plugin.py
```

## 2. Extend `DiscordCommandPlugin` instead of `PluginBase`

It's the same five-method contract, plus one more: `execute()`.

```python
# plugins/commands/echo/plugin.py
from typing import Any

from app.discord.command_plugin import DiscordCommandPlugin
from app.discord.dispatch import CommandContext, CommandResponse
from app.plugins.base import PluginHealth, PluginPermission


class EchoPlugin(DiscordCommandPlugin):
    """Replies with whatever the user asked."""

    name = "Echo"
    version = "0.1.0"
    category = "commands"
    command_name = "echo"                 # this becomes /echo
    command_description = "Repeats back a message."

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def health(self) -> PluginHealth:
        return PluginHealth(status="healthy")

    def config(self) -> dict[str, Any]:
        return {}

    def permissions(self) -> list[str]:
        return [PluginPermission.EVENTS_PUBLISH]

    async def execute(self, ctx: CommandContext) -> CommandResponse:
        return CommandResponse(content=f"You are user {ctx.user_id}")
```

That's the whole integration. The bot finds it via the same
`PluginRegistry` used for every other plugin category, registers it as a
slash command in `setup_hook()`, and every invocation is automatically
logged as a `CommandInvoked` event.

## 3. What's different from an indicator/scanner plugin

- `command_name` must be lowercase, 1-32 characters, letters/numbers/
  underscore/hyphen only (`app.discord.is_valid_command_name` — an invalid
  name is logged and skipped, not fatal).
- `execute()` receives a `CommandContext` (`user_id`, `guild_id`,
  `channel_id`, `args`) — never the raw discord.py `Interaction`. This is
  what keeps command plugins testable without a live Discord connection.
- `execute()` returns a `CommandResponse(content=..., ephemeral=...,
  buttons=...)`.
- If `execute()` raises, `dispatch_command` catches it, publishes
  `CommandFailed`, and returns a generic apology to the user — your plugin
  never needs its own top-level try/except for this.

## 3.5. Declaring parameters (`/analyze SYMBOL`, not just `/ping`)

A command with no parameters (like `Echo` above) is the whole story. A
command with a real argument declares it as data:

```python
from app.discord.command_plugin import CommandOption

class AnalyzePlugin(DiscordCommandPlugin):
    command_name = "analyze"
    parameters = (
        CommandOption(name="symbol", description="Ticker symbol, e.g. NVDA", required=True),
    )

    async def execute(self, ctx: CommandContext) -> CommandResponse:
        symbol = ctx.args["symbol"]
        ...
```

`ctx.args` is keyed by each option's `name`. `TradingBot` derives the real
discord.py slash-command option from this tuple at registration time — no
other integration step, same "drop it in" rule as everything else. Option
names follow the same naming rule as command names
(`app.discord.is_valid_option_name`); an invalid one is logged and the
whole command is skipped, not fatal. Every option is currently
string-typed — see `CommandOption`'s docstring in
`app/discord/command_plugin.py` before a command needs int/float/bool/
choice options.

## 3.6. Interactive buttons — declare actions, don't build buttons

Don't construct `CommandButton`s by hand. Import the Discord Action
Registry and declare which reusable actions your command wants:

```python
from app.discord.actions import ACTION_REGISTRY

_ACTIONS = ["chart", "news", "watch", "dismiss"]

return CommandResponse(
    content="...",
    buttons=ACTION_REGISTRY.buttons_for(_ACTIONS, target=symbol),
)
```

The registry (`app/discord/actions.py`) owns button creation, click
behavior, shared styling, and placeholder behavior — a command plugin
never builds a `CommandButton` or implements click handling itself.
`custom_id` convention: `"{action_key}:{target}"` (action-first, not
command-first — the same `"chart"` button behaves identically no matter
which command attached it). `"dismiss"` is the one action with a real
handler today (deletes the message); every other registered action
(chart/news/history/backtest/journal/watch/refresh/replay/coach) gets a
generic, honest "not built yet" reply until a real handler is registered
for it — see the module docstring for how a future milestone gives one of
them real behavior without touching any command plugin. See
`plugins/commands/analyze/plugin.py` (seven actions) and
`plugins/commands/scan/plugin.py` (two actions, Refresh + Dismiss) for
reference usage — the same registry, reused, is the point.

If you ever need a one-off button an action doesn't cover, `CommandButton`
(`label`, `custom_id`, `style`, `disabled`) is still a plain dataclass you
can construct directly — but reach for the registry first.

## 3.7. Reading current evidence/reasoning/market-data state (read-only, on demand)

Most plugins only ever react to events. A command like `/analyze` needs
something different: the *current* state, synchronously, right now — not
whatever the next event happens to publish. For exactly this case,
`PluginContext` carries several additional, optional references:
`context.evidence_aggregator`, `context.reasoning_engine`,
`context.strategy_engine`, `context.market_data_service`,
`context.plugin_registry`, `context.context_engine`, and
`context.portfolio_engine` (all default to `None` — handle that
gracefully, most unit tests won't supply them).

```python
async def execute(self, ctx: CommandContext) -> CommandResponse:
    aggregator = self.context.evidence_aggregator
    reasoning_engine = self.context.reasoning_engine
    if aggregator is None or reasoning_engine is None:
        return CommandResponse(content="Analysis isn't available right now.", ephemeral=True)

    snapshot = aggregator.snapshot(symbol)       # AggregateSnapshot
    output = await reasoning_engine.analyze(symbol)  # ReasoningOutput
    ...
```

This is a deliberate, narrow exception to "plugins only talk through the
Event Bus" (see `PluginContext`'s docstring in `app/plugins/base.py`) —
read-only queries only. Never use these to mutate state, publish on
another system's behalf, or reach into a specific indicator plugin's
internals.

`context.portfolio_engine` works the same way, for the Portfolio
Intelligence Layer's continuously-updated watchlist state:

```python
portfolio_engine = self.context.portfolio_engine
if portfolio_engine is None:
    return CommandResponse(content="The watchlist isn't available right now.", ephemeral=True)

profiles = portfolio_engine.ranked_watchlist()  # list[SymbolProfile], highest priority first
one = portfolio_engine.snapshot("NVDA")         # SymbolProfile | None -- None if not on the watchlist
```

See `plugins/commands/watchlist/plugin.py` for the full reference usage.

## 4. Test it without Discord

```python
async def test_echo_command(event_bus, settings):
    registry = PluginRegistry(event_bus, settings)
    await registry.load_all(PROJECT_ROOT)
    echo = registry.get("Echo")

    ctx = CommandContext(user_id="1", guild_id=None, channel_id=None, args={})
    response = await dispatch_command(echo, event_bus, ctx)

    assert response.content == "You are user 1"
```

See `tests/test_ping_plugin.py` and `tests/test_discord_dispatch.py` for
complete worked examples, including how `tests/test_discord_bot.py` verifies
the bridge to a real `discord.Interaction` with a lightweight fake object —
without ever needing a network connection to Discord. See
`tests/test_analyze_plugin.py` for a parameterized command exercising the
`evidence_aggregator`/`reasoning_engine` query pattern against real (not
mocked) components, and `tests/test_discord_bot.py`'s parameterized-option
and button tests for how a declared `CommandOption`/`CommandButton` becomes
a real discord.py option/component.

---

# Writing a strategy

A strategy is **pure data** — a YAML file, never a Python `plugin.py`. It
never references a specific indicator's implementation, only the evidence
titles that indicator happens to publish. This is what makes "no strategy
should require Python modifications to support new indicators" true:
dropping in a new indicator plugin makes its evidence titles usable in any
strategy's `required`/`optional` list immediately, with zero code changes.

## 1. Create the folder

```
plugins/strategies/momentum_breakout/
    strategy.yaml
```

## 2. Write the declarative definition

```yaml
# plugins/strategies/momentum_breakout/strategy.yaml
name: Momentum Breakout

required:
  - Bullish EMA Cross
  - Donchian Channel Breakout (New High)

optional:
  - Bullish SMA Cross
  - CCI Breakout Above 100

minimum_score: 32

repeat_policy:
  Donchian: after_pullback
```

- **`required`** — evidence titles (exact match, case-insensitive) that
  must ALL be present and fresh (per the Evidence Aggregator) for this
  strategy to be eligible to match at all.
- **`optional`** — evidence titles that aren't required, but whose `score`
  counts toward `minimum_score` when present. Lets a strategy reward extra
  confirmation without hard-requiring it.
- **`minimum_score`** — the summed `score` of present required + optional
  evidence must reach this. Missing any required title means the strategy
  can never match regardless of score.
- **`repeat_policy`** — maps an evidence *source* (not title) to how
  repeated occurrences from that source should be interpreted:
  `every_breakout` (default — accept any occurrence), `first_breakout`
  (only the first occurrence in its current sequence), or `after_pullback`
  (like `first_breakout`, but also excludes a cold-start occurrence with no
  real prior sequence behind it). This only has an effect on evidence that
  carries `is_first_in_sequence` / `is_first_ever` in its `metadata` (see
  `plugins/indicators/donchian/plugin.py` for the reference
  implementation of that convention) — evidence without it always passes,
  regardless of policy.

## 3. That's it — no Python, no registration step

`StrategyEngine.load()` scans `plugins/strategies/*/strategy.yaml` at
bootstrap and compiles each one into a `CompiledStrategy` (frozensets +
a score threshold, built once — never re-parsed per evaluation). A broken
YAML file is logged and skipped, the same isolation policy
`PluginRegistry.load_all` uses for a broken plugin — one strategy author's
typo can't take down every other strategy.

## 4. What you must never do

- Never write `if evidence.source == "EMA": ...` or reference any specific
  indicator's Python module from a strategy or from `app/strategy/` itself
  — a strategy only ever names evidence by `title` (and, for repeat
  handling, `source`), the same vocabulary any evidence producer speaks.
- Never expect `optional` entries to be required — they only add to the
  score total when present. If something must always be there, put it in
  `required`.

## 5. Test it

```python
from pathlib import Path
from app.strategy.loader import load_strategies
from app.strategy.engine import StrategyEngine

def test_momentum_breakout_loads():
    compiled = load_strategies(Path("plugins/strategies"))
    assert any(s.name == "Momentum Breakout" for s in compiled)
```

See `tests/test_strategy_engine.py` for compilation/evaluation/repeat-policy
tests, and `tests/test_pipeline_integration.py` for a full, real
Indicator → Evidence Aggregator → Strategy Engine → Reasoning Engine run
using this exact reference strategy.

---

# Writing a market data provider plugin

A market data provider is a plugin — live feed, replay engine, historical
database, paper trading feed, future broker API, all implementing the
exact same contract. The Scanner Engine never imports a specific
provider; adding one never requires touching `app/scanner/` or
`app/marketdata/`.

## 1. Create the folder and extend `MarketDataProviderPlugin`

```python
# plugins/market_data/my_feed/plugin.py
from typing import Any

from app.indicators.bar import Bar
from app.marketdata.provider import MarketDataProviderPlugin
from app.plugins.base import PluginHealth, PluginPermission


class MyFeedPlugin(MarketDataProviderPlugin):
    name = "MyFeed"
    version = "0.1.0"
    provider_name = "my_feed"  # matched against settings.market_data.providers

    async def initialize(self) -> None:
        ...  # open a connection, warm a cache, etc.

    async def shutdown(self) -> None:
        ...

    async def health(self) -> PluginHealth:
        return PluginHealth(status="healthy")

    def config(self) -> dict[str, Any]:
        return {}

    def permissions(self) -> list[str]:
        return [PluginPermission.MARKET_DATA_READ]

    async def fetch(self, symbols: list[str], timeframe: str) -> dict[str, Bar]:
        # Return the latest known Bar per symbol you currently have data
        # for -- omit a symbol you don't have yet, never raise for that.
        # Only raise for a genuine failure (connection down, query timed
        # out, ...); MarketDataService treats an exception as "this
        # provider is unavailable right now" and fails over to the next
        # configured provider.
        ...
```

## 2. Add it to `settings.market_data.providers`

```yaml
# config/default.yaml
market_data:
  providers: ["my_feed", "replay"]  # tried in this order
```

That's the whole integration — `MarketDataService` picks it up
automatically the next time the app boots, and the Scanner Engine gets its
data through it with zero changes to any scanner.

## 3. What you must never do

- Never let the Scanner Engine (or anything else) import your plugin
  module directly — the whole point of `MarketDataService` is that
  nothing downstream knows which provider answered `fetch()`.
- Never raise for "no data for this symbol yet" — omit it from the
  returned dict. Only raise for a genuine provider failure.
- If your provider is push-based under the hood (a websocket feed), cache
  the latest tick/bar per symbol internally and answer `fetch()` from that
  cache — `fetch()` itself must always be a fast, synchronous-feeling
  read, never a blocking wait for the next tick.

## 4. Test it

See `tests/test_marketdata.py` for `MarketDataService` failover/ordering
tests against a duck-typed stub provider, and
`plugins/market_data/replay/plugin.py` for the reference implementation
(CSV replay with a deterministic synthetic-random-walk fallback).

---

# Writing a scanner plugin

A scanner is the thing that makes the platform "continuous" — it
repeatedly asks the Market Data Abstraction Layer for the latest bar per
symbol/timeframe and publishes `MarketDataUpdated`. Indicator plugins
already subscribe to that event, so a scanner never calls one directly.

## 1. Almost always, just configuration

The base class, `ScannerPlugin` (`app/scanner/plugin.py`), already
implements the entire Universal Plugin Contract generically — a concrete
scanner plugin is usually nothing but a class declaration and a
`config.yaml`:

```python
# plugins/scanners/crypto/plugin.py
from app.scanner.plugin import ScannerPlugin


class CryptoWatchlistScanner(ScannerPlugin):
    """A second, independently-configured scanner -- proves multiple
    scanners can run simultaneously with zero shared-logic changes."""

    name = "CryptoWatchlistScanner"
    version = "0.1.0"
```

```yaml
# plugins/scanners/crypto/config.yaml
watchlist: ["BTC-USD", "ETH-USD"]
timeframes: ["1m", "5m"]
interval_seconds: 10
asset_class: "crypto"
```

That's it — no Python beyond the class declaration, no registration step.
`initialize()` starts a real background loop the moment this plugin
loads; `interval_seconds` (falling back to `settings.scanner.interval_seconds`
if omitted) controls how often it ticks.

## 2. Only override `scan_once()` for genuinely different behavior

The default `scan_once()` fetches every configured timeframe for the
whole watchlist and publishes one `MarketDataUpdated` per symbol/
timeframe pair. Override it only if a scanner needs different behavior
(e.g. splitting a huge watchlist across multiple `fetch()` calls) — even
then, keep publishing `MarketDataUpdated` as the only way data reaches
the rest of the system.

## 3. What you must never do

- Never call an indicator plugin, the Evidence Aggregator, or the
  Strategy Engine directly — publish `MarketDataUpdated` and let the
  Event Bus do the rest, exactly like every other producer of that event.
- Never import a specific market data provider module — read
  `context.market_data_service.fetch(...)` only (see `PluginContext`'s
  docstring in `app/plugins/base.py` for why this on-demand query is a
  documented exception, not a violation, of "plugins only talk through
  the Event Bus").

## 4. Test it

Construct a `ScannerPlugin` directly with a short `interval_seconds` and a
fake/stub `market_data_service` rather than waiting on the reference
config's real interval — see `tests/test_scanner_plugin.py`. See
`tests/test_scanner_pipeline_integration.py` for a full, real
Scanner → Market Data Abstraction Layer → Replay Provider →
Indicator Plugins → Evidence Aggregator → Strategy Engine → `/analyze`
run, ticking on a real background loop over real (compressed) wall-clock
time — the proof that continuous scanning, not hand-published events,
drives the whole pipeline.

Note: the reference scanner (`CoreWatchlistScanner`) is disabled by
default in the test suite's `settings` fixture (`tests/conftest.py`) so
the ~150+ other tests that load the full plugin registry don't each spin
up an unwanted long-running background task. Tests that want the real
reference scanner enabled restore `settings.plugins.disabled` themselves.

---

# Writing an External Intelligence Platform plugin

PROJECT.md's Milestone 7 spec is explicit: don't build a separate News
engine, a separate Earnings engine, a separate Macro engine. Every
non-price source of market information — news, earnings, SEC filings,
insider activity, FDA approvals, M&A, buybacks, dividends, stock splits,
economic releases, treasury auctions, Fed speeches, a future economic
calendar, anything — is a plugin under `plugins/intelligence/`,
implementing the exact same contract. The Evidence Aggregator can't tell
one from an indicator plugin, by design.

## 1. Almost always, just one method: `poll_once()`

The base class, `IntelligencePlugin` (`app/intelligence/plugin.py`),
already implements the entire Universal Plugin Contract generically plus
a config-driven polling loop (mirroring `ScannerPlugin`'s tick loop) — a
concrete plugin overrides `poll_once()` and calls `self._publish(...)`
for each new item it finds:

```python
# plugins/intelligence/sec_filings/plugin.py
from typing import Any

from app.event_bus.events import Event
from app.evidence.schema import Evidence, EvidenceCategory
from app.intelligence.plugin import IntelligencePlugin


class SecFilingReceived(Event):
    symbol: str
    filing_type: str  # "10-K", "10-Q", "8-K", "Form 4", ...
    url: str | None = None


class SecFilingsPlugin(IntelligencePlugin):
    """Polls for new SEC filings against the configured watchlist."""

    name = "SECFilings"
    version = "0.1.0"

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self.watchlist: tuple[str, ...] = tuple(context.plugin_config.get("watchlist") or [])

    async def poll_once(self) -> None:
        for symbol in self.watchlist:
            filing = await self._check_for_new_filing(symbol)  # your integration
            if filing is None:
                continue
            intelligence_event = SecFilingReceived(source=self.name, symbol=symbol, filing_type=filing.type, url=filing.url)
            evidence = Evidence(
                source=self.name,
                category=EvidenceCategory.NEWS,
                title=f"{symbol} filed a {filing.type}",
                score=15,
                confidence=75,
                direction="neutral",
                symbol=symbol,
                metadata={"filing_type": filing.type},
            )
            await self._publish(intelligence_event, evidence)
```

```yaml
# plugins/intelligence/sec_filings/config.yaml
watchlist: ["NVDA", "AAPL"]
interval_seconds: 300
```

That's the whole integration — no new subsystem, no core code touched.
Reusing an existing Intelligence Event schema (`NewsReceived`,
`EarningsReleased`, `MacroEventOccurred`) instead of defining a new one is
fine when your source's shape genuinely matches one; define a new `Event`
subclass (as above) when it doesn't.

## 2. Feeding the Market Context Engine (optional)

If your source is calendar/macro-flavored (a Fed meeting, a CPI release,
an earnings-season cluster, a market holiday), tag the evidence's
`metadata` with a `context_hint` key. The Market Context Engine
(`app/context/engine.py`) promotes any evidence carrying it into a
`MarketContextUpdated` event — a generic mechanism, not hardcoded per
source:

```python
evidence = Evidence(
    ...,
    metadata={"context_hint": "fed_week"},  # -> MarketContextUpdated(context_type="macro_event", label="Fed Week")
)
```

An unrecognized hint still gets promoted — the label falls back to a
title-cased version of the hint string itself (see
`_CONTEXT_HINT_LABELS` in `app/context/engine.py`), so you never need to
touch that file to add a new macro context type.

## 3. What you must never do

- Never generate a buy/sell recommendation — evidence only, the same
  vocabulary every other plugin in this codebase speaks.
- Never bypass the Event Bus — `self._publish(intelligence_event,
  evidence)` (or `self.context.event_bus.publish(...)` directly, for a
  bare intelligence event with no matching evidence yet) is the only way
  anything you do becomes visible.
- Never call a real external API without being honest about it if you
  can't (no network access, no credentials yet, ...) — see
  `plugins/intelligence/news/plugin.py`'s docstring for how the reference
  plugins handle this: clearly-labeled synthetic data
  (`provider: "synthetic-news-feed"`), deterministic per symbol via a
  stable seed, never presented as real.
- Never import another intelligence plugin's module — if two sources
  need to share logic, it belongs in `app/intelligence/plugin.py` (the
  shared base), not in a peer-to-peer import.

## 4. Test it

Drive `poll_once()` directly (many polls, since real-world sources fire
probabilistically) rather than waiting on the real interval — see
`tests/test_news_plugin.py`, `tests/test_earnings_plugin.py`,
`tests/test_macro_plugin.py`. See
`tests/test_milestone7_pipeline_integration.py` for a full, real
Indicator Plugins + External Intelligence Platform + Market Context
Engine + Confidence Weighting Framework → `/analyze` run.

Note: like the reference scanner, the reference News/Earnings/Macro
plugins are disabled by default in the test suite's `settings` fixture
(`tests/conftest.py`) for the same reason — each starts a real background
polling task the moment it's loaded. Tests that want them enabled restore
`settings.plugins.disabled` themselves, or construct the plugin directly
and drive `poll_once()` by hand.
