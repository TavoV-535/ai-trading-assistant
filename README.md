# AI Trading Assistant

An event-driven, plugin-first Discord trading intelligence platform. It
gathers market evidence, reasons about it with Claude, explains its
conclusions, and helps you test and improve strategies — it is explicitly
**not** a signal-selling bot. See [`PROJECT.md`](./PROJECT.md) for the full
product spec this codebase is built against.

Runs entirely on your own machine via Docker Compose.

## Status

**Milestone 1 — Core Architecture: complete.**

This milestone built the foundation everything else plugs into: the event
bus, the plugin contract, the evidence object, the reasoning engine, the
database layer, and local deployment. No Discord wiring, indicators beyond
one reference plugin, or trading domain models yet — those are later
milestones, built on top of this without changing any of it. See
[`docs/MILESTONES.md`](./docs/MILESTONES.md) for what's done and what's next.

## Quick start (Docker — recommended)

```bash
cp .env.example .env
# fill in ANTHROPIC_API_KEY if you want AI-generated summaries;
# without it the Reasoning Engine still runs in evidence-only mode.

./scripts/start.sh
# equivalent to: docker compose -f docker/docker-compose.yml up --build
```

This starts Postgres, runs Alembic migrations automatically, and starts the
app. Check it's alive:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/plugins
```

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

35 tests, ~92% coverage of `app/` as of Milestone 1.

## Project structure

```
app/
  config/       # pydantic-settings: YAML + env vars, never hardcoded
  logging/      # structlog + rotating file handlers
  event_bus/    # the async pub/sub bus + every core Event schema
  evidence/     # the Universal Evidence Object
  plugins/      # PluginBase contract + auto-discovery + registry
  reasoning/    # Reasoning Engine + Claude provider
  db/           # SQLAlchemy models, Repository pattern, event persistence
  core/         # bootstrap/teardown sequencing + FastAPI app (/health, /plugins)
plugins/        # actual plugins live here, auto-discovered — see docs/PLUGIN_GUIDE.md
  indicators/ema/
alembic/        # migrations (async, driven by app.config settings)
docker/         # Dockerfile, docker-compose.yml, entrypoint.sh
docs/           # architecture, plugin guide, milestone tracker
tests/          # pytest suite mirroring the app/ layout
```

## Documentation

- [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) — how the event bus, plugin contract, evidence object, and reasoning engine fit together
- [`docs/PLUGIN_GUIDE.md`](./docs/PLUGIN_GUIDE.md) — how to add a new plugin without touching core code
- [`docs/MILESTONES.md`](./docs/MILESTONES.md) — what's built, what's next, in the order `PROJECT.md` implies
- [`PROJECT.md`](./PROJECT.md) — the full product spec
