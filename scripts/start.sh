#!/usr/bin/env bash
# Single-command startup: docker compose up, building the app image if needed.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "No .env found — copying .env.example. Fill in your Discord token and API keys before continuing."
  cp .env.example .env
fi

docker compose -f docker/docker-compose.yml up --build
