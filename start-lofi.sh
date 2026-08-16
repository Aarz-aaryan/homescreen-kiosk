#!/bin/bash
# Start lofi stream via mpv through HDMI audio output
# Args:
#   $1 (optional) - stream URL (default: SomaFM Groove Salad)
STREAM_URL="${1:-https://ice1.somafm.com/groovesalad-128-mp3}"
STATION_NAME="${2:-SomaFM Groove Salad}"

# If a previous instance is running, kill it
pkill -f "mpv.*somafm\|mpv.*lofi\|mpv.*stream" 2>/dev/null || true
sleep 1

# Run mpv as kiosk user, no video, route to HDMI
exec /usr/bin/mpv \
  --no-video \
  --no-terminal \
  --no-input-terminal \
  --no-osc \
  --no-osd-bar \
  --audio-device=alsa/hdmi:CARD=HDMI,DEV=0 \
  --volume=70 \
  --cache=yes \
  --cache-secs=20 \
  --demuxer-readahead-secs=20 \
  --stream-buffer-size=4MiB \
  --idle=no \
  --force-window=no \
  --quiet \
  --msg-level=all=warn \
  --log-file=/home/kiosk/.mpv.log \
  "$STREAM_URL" 2>&1
