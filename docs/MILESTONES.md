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

## Proposed order for what's next

These map directly to `PROJECT.md` sections. Suggested build order —
open to reordering based on what you want to see working first:

1. **Discord bot skeleton** — bot connects, `/help`, command routing through
   the existing Event Bus (a Discord command becomes an event; a response
   is a subscriber reacting to it). Needs a bot token — see the walkthrough
   you asked for separately.
2. **Indicator library** — SMA, VWAP, RSI, MACD, ATR, ADX, Bollinger,
   Supertrend, OBV, CCI, Ichimoku, Donchian, Volume Profile, each as its own
   plugin following the EMA pattern (`docs/PLUGIN_GUIDE.md`).
3. **Strategy Engine** — YAML/JSON strategy recipes that reference evidence
   by category/source, a minimum-score gate, `StrategyMatched` events.
4. **`/analyze SYMBOL`** — the first real Discord command: pulls evidence +
   reasoning output for a symbol, renders as an interactive message with
   buttons (Chart / News / History / Backtest / Journal / Watch / Dismiss).
5. **Scanner Engine** — continuous per-minute evidence generation across a
   watchlist, multiple timeframes and asset classes.
6. **News / Earnings / Macro engines** — each a plugin category, each only
   ever publishing `NewsReceived` / `EarningsReleased` / evidence, never a
   directive.
7. **Watchlists**, then **Backtesting**, then **Journaling**, **Risk
   Engine**, **AI Coach**, **Replay Mode**, **Optimization Engine**,
   **Personal Statistics** — roughly in that order, since each leans on the
   ones before it (backtesting needs strategies + indicators; the coach
   needs journaling; risk warnings need trade events already flowing).

Say the word and the next milestone starts. Nothing here commits to a
specific order — just say which one you want first.
