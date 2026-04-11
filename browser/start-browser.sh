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

exec su -s /bin/bash browser -c '
export DISPLAY=:0
export HOME=/home/browser
export XDG_RUNTIME_DIR=/tmp/runtime-browser
eval "$(dbus-launch --sh-syntax)"
openbox --config-file /etc/xdg/openbox/rc.xml &
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
    --disable-features=TabHoverCardImages,TabGroupsSave,SidePanelPinning \
    --disable-session-crashed-bubble \
    --window-position=0,0 \
    --window-size=1024,576 \
    --disable-extensions-except=/opt/yflix-extension \
    --load-extension=/opt/yflix-extension \
    --app=https://yflix.to || true
  sleep 2
done'
