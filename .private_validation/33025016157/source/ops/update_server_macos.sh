#!/usr/bin/env bash
set -euo pipefail
ROOT="${POLYMARKET_APP_DIR:-$HOME/polymarket}"
exec bash "$ROOT/ops/update_server.sh" "$@"
