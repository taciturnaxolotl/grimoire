#!/usr/bin/env bash
set -euo pipefail

if [ $# -eq 0 ]; then
  echo "usage: $(basename "$0") <text>" >&2
  exit 1
fi

TEXT="$*"
TMP="/tmp/glossa_test.json"

.venv/bin/python3 glossa.py --json "$TMP" "$TEXT"
scp "$TMP" remarkable:/tmp/glossa_strokes.json
ssh -o ConnectTimeout=10 remarkable '/home/root/uinject /tmp/glossa_strokes.json'
