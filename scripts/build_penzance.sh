#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_TAR="${OUTPUT_TAR:-/tmp/isambard-images.tar}"
REMOTE_HOST="${REMOTE_HOST:-penzance}"

cd "$ROOT_DIR"

docker buildx build --platform linux/amd64 -t isambard-app:latest -f Dockerfile --load .
docker buildx build --platform linux/amd64 -t isambard-browser:latest -f browser.Dockerfile --load .
docker save -o "$OUTPUT_TAR" isambard-app:latest isambard-browser:latest
scp "$OUTPUT_TAR" "${REMOTE_HOST}:~/isambard-images.tar"
scp compose.yaml "${REMOTE_HOST}:~/compose.yaml"
scp guacamole/user-mapping-gluetun.xml "${REMOTE_HOST}:~/user-mapping.xml"

echo "Copied images and compose files to ${REMOTE_HOST}."
echo "On ${REMOTE_HOST}, run:"
echo "  mkdir -p /data/crimson/config/isambard/guacamole"
echo "  mv ~/user-mapping.xml /data/crimson/config/isambard/guacamole/user-mapping.xml"
echo "  sudo docker load -i ~/isambard-images.tar"
echo "  sudo docker compose -f ~/compose.yaml up -d isambard-app isambard-guacd isambard-guacamole isambard-browser"
