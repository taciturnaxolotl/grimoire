#!/bin/bash
# Build and deploy all glossa components to reMarkable
# Usage: bash build.sh [--no-restart]
set -e
cd "$(dirname "$0")"

CONTAINER="glossa-builder"
EXT_DIR="$(pwd)/glossa-injector"
UINJECT_DIR="$(pwd)/uinject"
UINJECTD_DIR="$(pwd)/uinjectd"
GLOSSAD_DIR="$(pwd)/glossad"
FONT_FILE="$(dirname "$0")/../fonts/EMSAllure.svg"
RESTART_XOCHITL=true

if [ "$1" = "--no-restart" ]; then
    RESTART_XOCHITL=false
fi

# ─── Builder container ──────────────────────────────────────────────

if ! docker inspect $CONTAINER &>/dev/null; then
    echo "Creating builder container..."
    docker create --platform linux/amd64 --name $CONTAINER \
        -v "$(pwd):/xovi-ext" \
        -w /xovi-ext/glossa-injector \
        eeems/remarkable-toolchain:latest-rm2 \
        bash -c "git clone https://github.com/asivery/xovi /tmp/xovi 2>/dev/null; tail -f /dev/null"
    docker start $CONTAINER
    sleep 3
fi

# ─── Build extension .so ────────────────────────────────────────────

echo "=== Building glossa-injector.so ==="
docker exec $CONTAINER bash -c '
export XOVI_REPO=/tmp/xovi
. /opt/codex/*/*/environment-setup-*
cd /xovi-ext/glossa-injector
qmake6 2>/dev/null
make 2>&1
'
echo "Copying .so..."
docker cp $CONTAINER:/xovi-ext/glossa-injector/glossa-injector.so "$EXT_DIR/"

# ─── Build uinject ──────────────────────────────────────────────────

echo "=== Building uinject ==="
docker exec $CONTAINER bash -c '
. /opt/codex/*/*/environment-setup-*
cd /xovi-ext/uinject
$CC -o uinject uinject.c -O2 $CFLAGS $LDFLAGS
echo "uinject built OK"
'
echo "Copying uinject..."
docker cp $CONTAINER:/xovi-ext/uinject/uinject "$UINJECT_DIR/"

# ─── Build uinjectd ─────────────────────────────────────────────────

echo "=== Building uinjectd ==="
docker exec $CONTAINER bash -c '
. /opt/codex/*/*/environment-setup-*
cd /xovi-ext/uinjectd
$CC -o uinjectd uinjectd.c -O2 $CFLAGS $LDFLAGS -lpthread
echo "uinjectd built OK"
'
echo "Copying uinjectd..."
docker cp $CONTAINER:/xovi-ext/uinjectd/uinjectd "$UINJECTD_DIR/"

# ─── Build glossad ────────────────────────────────────────────────

echo "=== Building glossad ==="
docker exec $CONTAINER bash -c '
. /opt/codex/*/*/environment-setup-*
cd /xovi-ext/glossad
$CC -o glossad glossad.c font_render.c -O2 $CFLAGS $LDFLAGS -lpthread -lpng16 -lssl -lcrypto -lm
echo "glossad built OK"
'
echo "Copying glossad..."
docker cp $CONTAINER:/xovi-ext/glossad/glossad "$GLOSSAD_DIR/"

# ─── Deploy to device ───────────────────────────────────────────────

echo "=== Deploying to device ==="

# Stop services before replacing binaries
ssh remarkable 'systemctl stop glossad 2>/dev/null; killall uinject 2>/dev/null; killall uinjectd 2>/dev/null; killall glossad 2>/dev/null'
ssh remarkable 'rm -f /home/root/uinject /home/root/uinjectd /home/root/glossad'

# Copy binaries
scp "$EXT_DIR/glossa-injector.so" remarkable:/home/root/xovi/extensions.d/
scp "$UINJECT_DIR/uinject" remarkable:/home/root/uinject
scp "$UINJECTD_DIR/uinjectd" remarkable:/home/root/uinjectd
scp "$GLOSSAD_DIR/glossad" remarkable:/home/root/glossad

# Copy data files
scp "$FONT_FILE" remarkable:/home/root/EMSAllure.svg
scp "$GLOSSAD_DIR/font_data.json" remarkable:/home/root/font_data.json
scp "$GLOSSAD_DIR/ornaments.json" remarkable:/home/root/ornaments.json

# Deploy QML patches
echo "Deploying QML patches..."
scp "$(dirname "$0")/glossa_toggle.qmd" remarkable:/home/root/xovi/exthome/qt-resource-rebuilder/rmHacks/glossa_toggle.qmd
ssh remarkable 'grep -q glossa_toggle /home/root/xovi/exthome/qt-resource-rebuilder/zz_rmhacks.qmd || echo "LOAD rmHacks/glossa_toggle.qmd" >> /home/root/xovi/exthome/qt-resource-rebuilder/zz_rmhacks.qmd'

# Install systemd service for glossad
echo "Installing systemd service..."
ssh remarkable 'cat > /etc/systemd/system/glossad.service << SVCEOF
[Unit]
Description=Glossa on-device daemon
After=xochitl.service
Wants=xochitl.service

[Service]
Type=simple
ExecStart=/home/root/glossad
Restart=on-failure
RestartSec=3
StandardOutput=append:/tmp/glossad.log
StandardError=append:/tmp/glossad.log

[Install]
WantedBy=multi-user.target
SVCEOF
systemctl daemon-reload
systemctl enable glossad'

# Start services
ssh remarkable '/home/root/uinjectd &'
ssh remarkable 'systemctl start glossad'

# Restart xochitl if needed (for new .so or .qmd changes)
if [ "$RESTART_XOCHITL" = true ]; then
    echo "Restarting xochitl..."
    ssh remarkable 'systemctl reset-failed xochitl && systemctl restart xochitl'
    sleep 5
    # Re-start glossad after xochitl restart (systemd handles this but be safe)
    ssh remarkable 'systemctl restart glossad 2>/dev/null'
fi

# Verify
echo ""
echo "=== Status ==="
ssh remarkable 'systemctl is-active glossad 2>/dev/null && echo "glossad: running" || echo "glossad: NOT running"'
ssh remarkable 'ps | grep uinjectd | grep -v grep | head -n 1 | awk "{print \"uinjectd: running (pid \" \$1 \")\"}"'
ssh remarkable 'tail -n 1 /tmp/glossad.log 2>/dev/null'

echo ""
echo "Done!"
