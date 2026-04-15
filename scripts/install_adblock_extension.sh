#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="${ROOT_DIR}/browser_adblock_extension"
TMP_DIR="$(mktemp -d)"
ZIP_URLS=(
  "https://github.com/uBlockOrigin/uBOL-home/archive/refs/heads/main.zip"
  "https://github.com/uBlockOrigin/uBOL-home/archive/refs/heads/master.zip"
)

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

mkdir -p "$TARGET_DIR"

ZIP_PATH="${TMP_DIR}/ubol.zip"
for url in "${ZIP_URLS[@]}"; do
  if curl -fsSL "$url" -o "$ZIP_PATH"; then
    break
  fi
done

if [ ! -s "$ZIP_PATH" ]; then
  echo "Failed to download uBOL source archive." >&2
  exit 1
fi

unzip -q "$ZIP_PATH" -d "$TMP_DIR"

SOURCE_DIR="$(find "$TMP_DIR" -type f -name manifest.json -path '*/chromium/manifest.json' -print -quit)"
if [ -z "$SOURCE_DIR" ]; then
  echo "Unable to find chromium/manifest.json in downloaded archive." >&2
  exit 1
fi

SOURCE_DIR="$(dirname "$SOURCE_DIR")"

find "$TARGET_DIR" -mindepth 1 -maxdepth 1 \
  ! -name '.gitkeep' \
  ! -name 'README.md' \
  -exec rm -rf {} +

cp -R "$SOURCE_DIR"/. "$TARGET_DIR"/

if [ ! -f "$TARGET_DIR/manifest.json" ]; then
  echo "Install failed: manifest.json missing from target directory." >&2
  exit 1
fi

echo "Installed Chromium ad-block extension into:"
echo "  $TARGET_DIR"
echo
echo "Next:"
echo "  docker compose build browser"
echo "  docker compose up"
