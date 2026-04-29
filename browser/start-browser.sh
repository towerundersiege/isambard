#!/bin/bash
set -eu

export DISPLAY=:0
export HOME=/home/browser
export XDG_RUNTIME_DIR=/tmp/runtime-browser

mkdir -p /home/browser/downloads /home/browser/chromium-profile "$XDG_RUNTIME_DIR"
chown -R browser:browser /home/browser/downloads /home/browser/chromium-profile "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"

Xvfb :0 -screen 0 1024x576x16 -ac +extension RANDR &

while [ ! -S /tmp/.X11-unix/X0 ]; do
  sleep 0.2
done

sleep 1

x11vnc -display :0 -forever -shared -rfbport 5900 -passwd browserpass -xkb -repeat -wait 10 -noxdamage -noxfixes -noxrandr &

wait_for_mullvad() {
  while true; do
    if python3 - <<'PY'
import json
import sys
import urllib.request

try:
    with urllib.request.urlopen("https://am.i.mullvad.net/json", timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))
except Exception:
    sys.exit(1)

sys.exit(0 if payload.get("mullvad_exit_ip") else 1)
PY
    then
      break
    fi
    echo "Waiting for Mullvad connection before launching Chromium..."
    sleep 5
  done
}

wait_for_mullvad

exec su -s /bin/bash browser -c '
export DISPLAY=:0
export HOME=/home/browser
export XDG_RUNTIME_DIR=/tmp/runtime-browser
eval "$(dbus-launch --sh-syntax)"
openbox --config-file /etc/xdg/openbox/rc.xml &
EXTENSION_PATHS="/opt/yflix-extension"
if [ -f /opt/adblock-extension/manifest.json ]; then
  EXTENSION_PATHS="/opt/yflix-extension,/opt/adblock-extension"
fi
while true; do
  chromium \
    --no-sandbox \
    --disable-dev-shm-usage \
    --disable-gpu \
    --disable-software-rasterizer \
    --disable-background-networking \
    --disable-component-update \
    --disable-sync \
    --user-data-dir=/home/browser/chromium-profile \
    --no-first-run \
    --no-default-browser-check \
    --autoplay-policy=no-user-gesture-required \
    --disable-features=HttpsUpgrades,HttpsFirstModeV2,HttpsFirstBalancedModeAutoEnable,TabHoverCardImages,TabGroupsSave,SidePanelPinning \
    --disable-session-crashed-bubble \
    --window-position=0,0 \
    --window-size=1024,576 \
    --disable-extensions-except="${EXTENSION_PATHS}" \
    --load-extension="${EXTENSION_PATHS}" \
    https://yflix.to || true
  sleep 2
done'
