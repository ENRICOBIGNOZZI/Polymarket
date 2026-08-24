#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CONFIG="${CROSS_VENUE_CONFIG:-$ROOT/config/cross_venue.json}"
RUN_DIR="${CROSS_VENUE_RUN_DIR:-$ROOT/runs/cross_venue}"
PAIRS="${CROSS_VENUE_PAIRS:-$ROOT/config/cross_venue_pairs.csv}"
CREDENTIALS="${CROSS_VENUE_CREDENTIALS:-$HOME/.config/polymarket/venues.env}"
BINARY="${CROSS_VENUE_BINARY:-$ROOT/build/prediction_cross_venue_arb}"
AUTH_BINARY="${CROSS_VENUE_AUTH_BINARY:-$ROOT/build/prediction_cross_venue_auth_check}"
REQUIRE_AUTH="${CROSS_VENUE_REQUIRE_AUTH:-1}"

mkdir -p "$RUN_DIR"
[[ -x "$BINARY" ]] || { echo "fatal: cross-venue binary missing: $BINARY" >&2; exit 1; }
[[ -x "$AUTH_BINARY" ]] || { echo "fatal: auth-check binary missing: $AUTH_BINARY" >&2; exit 1; }
[[ -f "$CONFIG" ]] || { echo "fatal: cross-venue config missing: $CONFIG" >&2; exit 1; }
[[ -f "$PAIRS" ]] || { echo "fatal: pair manifest missing: $PAIRS" >&2; exit 1; }
[[ -f "$CREDENTIALS" ]] || { echo "fatal: credentials file missing: $CREDENTIALS" >&2; exit 1; }

if [[ "$REQUIRE_AUTH" == "1" ]]; then
  "$AUTH_BINARY" --config "$CONFIG" --credentials "$CREDENTIALS" \
    > "$RUN_DIR/auth_check.json"
fi

exec "$BINARY" \
  --config "$CONFIG" \
  --run-dir "$RUN_DIR" \
  --pairs "$PAIRS" \
  --credentials "$CREDENTIALS" \
  --loop
