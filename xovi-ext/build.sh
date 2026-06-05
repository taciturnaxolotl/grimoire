#!/bin/bash
# Build all grimoire xovi components: extension .so + uinject binary
set -e
cd "$(dirname "$0")"

CONTAINER="grimoire-builder"
EXT_DIR="$(pwd)/grimoire-injector"
UINJECT_DIR="$(pwd)/uinject"

# Create persistent builder container if needed
if ! docker inspect $CONTAINER &>/dev/null; then
    echo "Creating builder container..."
    docker create --platform linux/amd64 --name $CONTAINER \
        -v "$(pwd):/xovi-ext" \
        -w /xovi-ext/grimoire-injector \
        eeems/remarkable-toolchain:latest-rm2 \
        bash -c "git clone https://github.com/asivery/xovi /tmp/xovi 2>/dev/null; tail -f /dev/null"
    docker start $CONTAINER
    sleep 3
fi

# Build extension .so
echo "=== Building grimoire-injector.so ==="
docker exec $CONTAINER bash -c '
export XOVI_REPO=/tmp/xovi
. /opt/codex/*/*/environment-setup-*
cd /xovi-ext/grimoire-injector
qmake6 2>/dev/null
make 2>&1
'

echo "Copying .so..."
docker cp $CONTAINER:/xovi-ext/grimoire-injector/grimoire-injector.so "$EXT_DIR/"

# Build uinject
echo "=== Building uinject ==="
docker exec $CONTAINER bash -c '
. /opt/codex/*/*/environment-setup-*
cd /xovi-ext/uinject
$CC -o uinject uinject.c -O2 $CFLAGS $LDFLAGS
echo "uinject built OK"
'

echo "Copying uinject..."
docker cp $CONTAINER:/xovi-ext/uinject/uinject "$UINJECT_DIR/"

# Deploy both to device
echo "=== Deploying to device ==="
scp "$EXT_DIR/grimoire-injector.so" remarkable:/home/root/xovi/extensions.d/
scp "$UINJECT_DIR/uinject" remarkable:/home/root/uinject

echo "Done! Restart xochitl to load new extension."
