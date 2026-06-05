#!/bin/bash
# Fast rebuild: reuses container, only recompiles changed files
set -e
cd "$(dirname "$0")"

CONTAINER="grimoire-builder"

# Create persistent builder container if needed
if ! docker inspect $CONTAINER &>/dev/null; then
    echo "Creating builder container..."
    docker create --platform linux/amd64 --name $CONTAINER \
        -v "$(pwd):/src" \
        -w /src \
        eeems/remarkable-toolchain:latest-rm2 \
        bash -c "git clone https://github.com/asivery/xovi /tmp/xovi 2>/dev/null; tail -f /dev/null"
    docker start $CONTAINER
    sleep 3
fi

echo "Building..."
docker exec $CONTAINER bash -c '
export XOVI_REPO=/tmp/xovi
. /opt/codex/*/*/environment-setup-*
qmake6 2>/dev/null
make 2>&1
' 

echo "Copying .so..."
docker cp $CONTAINER:/src/grimoire-injector.so .

echo "Deploying to device..."
scp grimoire-injector.so remarkable:/home/root/xovi/extensions.d/

echo "Done! Triple-tap to reload."
