#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOMELAB_DIR="${HOMELAB_DIR:-$HOME/Projects/homelab}"
PASS_STORE_DIR="${PASS_STORE_DIR:-$HOMELAB_DIR/.homelab-pass}"

read_pass() {
  PASSWORD_STORE_DIR="$PASS_STORE_DIR" pass show "$1" | head -n 1
}

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required but not installed or not on PATH" >&2
  exit 1
fi

if ! command -v pass >/dev/null 2>&1; then
  echo "pass is required to read the homelab secrets store" >&2
  exit 1
fi

if [[ ! -d "$PASS_STORE_DIR" ]]; then
  echo "pass store not found at $PASS_STORE_DIR" >&2
  exit 1
fi

export JELLYFIN_URL="${JELLYFIN_URL:-https://jellyfin.towerundersiege.com}"
export JELLYFIN_API_KEY="${JELLYFIN_API_KEY:-$(read_pass homelab/isambard/jellyfin_api_key)}"
export TMDB_API_KEY="${TMDB_API_KEY:-$(read_pass homelab/isambard/tmdb_api_key)}"
export ISAMBARD_BROWSER_URL="${ISAMBARD_BROWSER_URL:-http://localhost:${ISAMBARD_BROWSER_PORT:-8766}}"
export ISAMBARD_BROWSER_USER="${ISAMBARD_BROWSER_USER:-guac}"
export ISAMBARD_BROWSER_PASSWORD="${ISAMBARD_BROWSER_PASSWORD:-guac}"

cd "$ROOT_DIR"

echo "Starting local Isambard stack from $ROOT_DIR"
echo "Jellyfin URL: $JELLYFIN_URL"
echo "Browser URL: $ISAMBARD_BROWSER_URL"

exec docker compose -f docker-compose.yml up --build "$@"
