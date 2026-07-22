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

## Proposed order for what's next

These map directly to `PROJECT.md` sections. Suggested build order —
open to reordering based on what you want to see working first:

1. **Indicator library** — SMA, VWAP, RSI, MACD, ATR, ADX, Bollinger,
   Supertrend, OBV, CCI, Ichimoku, Donchian, Volume Profile, each as its own
   plugin following the EMA pattern (`docs/PLUGIN_GUIDE.md`).
2. **Strategy Engine** — YAML/JSON strategy recipes that reference evidence
   by category/source, a minimum-score gate, `StrategyMatched` events.
3. **`/analyze SYMBOL`** — the first command with a real parameter (needs a
   small extension to `DiscordCommandPlugin` for declaring slash-command
   options): pulls evidence + reasoning output for a symbol, renders as an
   interactive message with buttons (Chart / News / History / Backtest /
   Journal / Watch / Dismiss).
4. **Scanner Engine** — continuous per-minute evidence generation across a
   watchlist, multiple timeframes and asset classes.
5. **News / Earnings / Macro engines** — each a plugin category, each only
   ever publishing `NewsReceived` / `EarningsReleased` / evidence, never a
   directive.
6. **Watchlists**, then **Backtesting**, then **Journaling**, **Risk
   Engine**, **AI Coach**, **Replay Mode**, **Optimization Engine**,
   **Personal Statistics** — roughly in that order, since each leans on the
   ones before it (backtesting needs strategies + indicators; the coach
   needs journaling; risk warnings need trade events already flowing).

Say the word and the next milestone starts. Nothing here commits to a
specific order — just say which one you want first.
