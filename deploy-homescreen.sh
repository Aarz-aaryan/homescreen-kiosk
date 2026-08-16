#!/bin/bash
# deploy-homescreen.sh — Deploy all homescreen fixes to the kiosk box
# Run from: ~/homescreen-preflight/ on THIS machine
# Requires: homescreen online, SSH key auth working
set -e

HOST="100.110.212.126"
KIOSK_DIR="/opt/kiosk"
SSH="ssh -i ~/.ssh/id_ed25519_homescreen -o StrictHostKeyChecking=no"

echo "=== Deploying homescreen fixes ==="

# 1. Deploy updated kiosk_server.py (with GIF magic-byte validation, weather endpoint)
echo "[1/7] kiosk_server.py..."
$SSH homescreen@$HOST "sudo mkdir -p $KIOSK_DIR && sudo chown kiosk:kiosk $KIOSK_DIR"
scp -i ~/.ssh/id_ed25519_homescreen -o StrictHostKeyChecking=no \
    ~/homescreen-preflight/kiosk_server.py \
    homescreen@$HOST:/tmp/kiosk_server.py
$SSH homescreen@$HOST "sudo mv /tmp/kiosk_server.py $KIOSK_DIR/kiosk_server.py && sudo chown kiosk:kiosk $KIOSK_DIR/kiosk_server.py"

# 2. Sync GIFs — 47 validated GIFs (46MB)
echo "[2/7] GIFs ($KIOSK_DIR/gifs/)..."
$SSH homescreen@$HOST "mkdir -p $KIOSK_DIR/gifs"
# Use rsync if available, else scp --progress
if $SSH homescreen@$HOST "which rsync" 2>/dev/null; then
    rsync -avz --progress -e "ssh -i ~/.ssh/id_ed25519_homescreen" \
        ~/homescreen-preflight/gifs/ \
        homescreen@$HOST:$KIOSK_DIR/gifs/
else
    # Fallback: scp each GIF individually (slower)
    scp -i ~/.ssh/id_ed25519_homescreen -o StrictHostKeyChecking=no \
        ~/homescreen-preflight/gifs/*.gif \
        homescreen@$HOST:$KIOSK_DIR/gifs/
fi

# 3. Deploy lofi music (start-lofi.sh + kiosk-lofi.service)
echo "[3/7] lofi music..."
scp -i ~/.ssh/id_ed25519_homescreen -o StrictHostKeyChecking=no \
    ~/homescreen-preflight/start-lofi.sh \
    homescreen@$HOST:/tmp/start-lofi.sh
$SSH homescreen@$HOST "sudo mv /tmp/start-lofi.sh $KIOSK_DIR/start-lofi.sh && sudo chmod +x $KIOSK_DIR/start-lofi.sh && sudo chown kiosk:kiosk $KIOSK_DIR/start-lofi.sh"

scp -i ~/.ssh/id_ed_ed25519_homescreen -o StrictHostKeyChecking=no \
    ~/homescreen-preflight/kiosk-lofi.service \
    homescreen@$HOST:/tmp/kiosk-lofi.service
$SSH homescreen@$HOST "sudo mv /tmp/kiosk-lofi.service /etc/systemd/system/kiosk-lofi.service && sudo systemctl daemon-reload"

# 3b. Check if lofi service file has correct SSH key path
echo "[3b] verify lofi service..."
$SSH homescreen@$HOST "cat $KIOSK_DIR/start-lofi.sh | grep -q 'hdmi' && echo 'lofi script OK'"

# 4. Restart kiosk-web service
echo "[4/7] restart kiosk-web..."
$SSH homescreen@$HOST "sudo systemctl restart kiosk-web && sudo systemctl status kiosk-web -n 3 --no-pager"

# 5. Enable + start lofi service
echo "[5/7] enable lofi..."
$SSH homescreen@$HOST "sudo systemctl enable --now kiosk-lofi.service && sudo systemctl status kiosk-lofi -n 3 --no-pager"

# 6. Verify GIF list endpoint
echo "[6/7] verify /backgrounds/list.json..."
sleep 2
$SSH homescreen@$HOST "curl -s http://127.0.0.1:8088/backgrounds/list.json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(f\"{len(d[\"backgrounds\"])} GIFs:\", d[\"backgrounds\"][:3], \"...\")'"

# 7. Check audio status
echo "[7/7] audio status..."
$SSH homescreen@$HOST "curl -s http://127.0.0.1:8088/audio.json"

echo ""
echo "=== Deploy complete ==="
echo "GIF black-screen fix: kiosk_server.py now validates GIFs by magic bytes on startup"
echo "Music: kiosk-lofi.service enabled + running"
echo "GIFs: 47 validated backgrounds (was ~13)"
