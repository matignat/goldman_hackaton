#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker CLI is required. Install Docker Desktop or a Docker-compatible engine first." >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is not running. Start Docker Desktop or Colima first." >&2
  exit 1
fi
if [[ ! -f .env ]]; then
  cp .env.example .env
  if command -v openssl >/dev/null; then
    key="$(openssl rand -hex 32)"
  else
    key="$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
  fi
  sed -i.bak "s/REPLACE_WITH_LONG_RANDOM_SECRET/$key/" .env
  rm -f .env.bak
  chmod 600 .env
  echo "Created .env with a random demo session secret."
fi
docker compose up --build
