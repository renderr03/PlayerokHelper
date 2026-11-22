#!/usr/bin/env bash
set -euo pipefail
SHOPBOT_PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$SHOPBOT_PROJECT_DIR"
if [[ ! -x .venv/bin/playerokhelper ]]; then
    echo "Сначала выполните: bash install.sh" >&2
    exit 1
fi
exec .venv/bin/playerokhelper run
