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
                                            ├→ Reasoning Engine
                                            └→ Database
                                                   │
                                        Discord Responses
```

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
`EarningsReleased`, `TradeOpened`, `TradeClosed`, `PositionUpdated`,
`WatchlistTriggered`, `StrategyMatched`, `BacktestFinished`,
`JournalCreated`, `DailySummary`, `RiskWarning`, plus `EvidenceProduced`
(wraps an `Evidence` object — see below). Every event carries `event_id`,
`timestamp`, `source`, and an optional `correlation_id` for tracing a chain
of related events (e.g. a trade's full lifecycle).

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

## Reasoning Engine (`app/reasoning/`)

Subscribes to `EvidenceProduced`, accumulates evidence per symbol (bounded
per-symbol buffer), and on `analyze(symbol)` synthesizes everything
gathered so far into a `ReasoningOutput`: market summary, trade thesis
(framed as a hypothesis, never a directive), risk assessment, alternative
scenario, confidence, suggested strategy archetypes, historical similarity.

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

## Core / lifecycle (`app/core/`)

`bootstrap()` brings systems up in dependency order (logging → event bus →
database → reasoning engine → plugin registry) and `teardown()` reverses
it. `create_app()` wires both into a FastAPI ASGI [`lifespan`](https://fastapi.tiangolo.com/advanced/events/),
which is also how **graceful shutdown** works: uvicorn intercepts
SIGINT/SIGTERM, runs the lifespan shutdown phase, and only then exits — so
`docker compose stop` always tears plugins, the event bus, and the database
down cleanly before the container exits.

- `GET /health` — overall status, DB reachability, per-plugin health
- `GET /plugins` — loaded plugin metadata + any that failed to load

## Configuration (`app/config/`)

`config/default.yaml` holds every non-secret behavior. `.env` /
environment variables hold secrets and per-deployment overrides, and always
win. Nothing is hardcoded — see `app/config/settings.py` for the full,
typed schema (`Settings`, cached via `get_settings()`).
