#!/bin/bash
# Launched by lightdm as the kiosk user.
# 1. Disable screen blanking
# 2. Hide cursor (background)
# 3. Wait for the preview web server
# 4. Launch chrome in kiosk mode

set +e

# Disable X screen saver and DPMS
xset s off 2>/dev/null
xset -dpms 2>/dev/null
xset s noblank 2>/dev/null

# Hide cursor after 1s idle (background)
nohup unclutter -idle 1 -root >/dev/null 2>&1 &

# Wait for the preview server (up to 30s)
for i in $(seq 1 30); do
  if curl -sf http://127.0.0.1:8088/ >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

# Launch chrome (foreground - this IS the session)
exec /usr/bin/google-chrome \
  --kiosk \
  --no-sandbox \
  --no-first-run \
  --no-default-browser-check \
  --disable-features=TranslateUI,InfiniteSessionRestore \
  --disable-pinch \
  --overscroll-history-navigation=0 \
  --check-for-update-interval=31536000 \
  --user-data-dir=/home/kiosk/.chrome \
  --disable-dev-shm-usage \
  http://127.0.0.1:8088/