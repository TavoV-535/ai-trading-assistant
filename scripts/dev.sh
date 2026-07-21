#!/usr/bin/env bash
# Local (non-Docker) development run — requires a Postgres reachable at
# DATABASE_URL (e.g. `docker compose -f docker/docker-compose.yml up postgres`).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -e ".[dev]" -q

alembic upgrade head
uvicorn app.core.app:create_app --factory --reload --host 0.0.0.0 --port 8000
