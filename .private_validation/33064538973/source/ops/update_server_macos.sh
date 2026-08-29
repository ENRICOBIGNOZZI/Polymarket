#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${POLYMARKET_APP_DIR:-$HOME/polymarket}"
EXPECTED_SHA="${EXPECTED_VALIDATED_SHA:-${POLYMARKET_EXPECTED_SHA:-}}"
[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "fatal: exact EXPECTED_VALIDATED_SHA is required" >&2; exit 78; }
[[ "$(uname -s)" == "Darwin" ]] || { echo "fatal: macOS wrapper requires Darwin" >&2; exit 78; }
cd "$APP_DIR"
git fetch --no-tags origin main paper-validated
[[ "$(git rev-parse origin/main)" == "$EXPECTED_SHA" ]] || { echo "fatal: origin/main != expected SHA" >&2; exit 78; }
[[ "$(git rev-parse origin/paper-validated)" == "$EXPECTED_SHA" ]] || { echo "fatal: paper-validated != expected SHA" >&2; exit 78; }
updater="$(mktemp)"
trap 'rm -f "$updater"' EXIT
git show "$EXPECTED_SHA:ops/update_server_v7.sh" > "$updater"
chmod 700 "$updater"
POLYMARKET_APP_DIR="$APP_DIR" EXPECTED_VALIDATED_SHA="$EXPECTED_SHA" POLYMARKET_DEPLOY_REF=paper-validated bash "$updater"
