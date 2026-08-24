#!/usr/bin/env bash
set -euo pipefail

CONFIG_DIR="${POLYMARKET_CONFIG_DIR:-$HOME/.config/polymarket}"
ENV_FILE="${CROSS_VENUE_CREDENTIALS:-$CONFIG_DIR/venues.env}"
KALSHI_KEY_FILE="${KALSHI_PRIVATE_KEY_DESTINATION:-$CONFIG_DIR/kalshi-private-key.pem}"
mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"

tmp_key="$(mktemp "$CONFIG_DIR/.kalshi-key.XXXXXX")"
tmp_env="$(mktemp "$CONFIG_DIR/.venues.XXXXXX")"
cleanup() { rm -f "$tmp_key" "$tmp_env"; }
trap cleanup EXIT

kalshi_key_id="${KALSHI_API_KEY_ID:-${KALSHI_KEY_ID:-}}"
if [[ -z "$kalshi_key_id" ]]; then
  read -r -p "Kalshi API key ID: " kalshi_key_id
fi
[[ -n "$kalshi_key_id" ]] || { echo "fatal: Kalshi key ID is empty" >&2; exit 1; }

if [[ -n "${KALSHI_PRIVATE_KEY_SOURCE:-}" ]]; then
  cp "$KALSHI_PRIVATE_KEY_SOURCE" "$tmp_key"
elif [[ ! -t 0 ]]; then
  cat > "$tmp_key"
else
  echo "Paste the Kalshi RSA private key, including BEGIN/END lines." >&2
  echo "Input stops automatically at the END PRIVATE KEY line." >&2
  : > "$tmp_key"
  while IFS= read -r line; do
    printf '%s\n' "$line" >> "$tmp_key"
    if [[ "$line" == *"END RSA PRIVATE KEY"* || "$line" == *"END PRIVATE KEY"* ]]; then
      break
    fi
  done
fi

chmod 600 "$tmp_key"
openssl pkey -in "$tmp_key" -noout -check >/dev/null
mv "$tmp_key" "$KALSHI_KEY_FILE"
chmod 600 "$KALSHI_KEY_FILE"

if [[ -f "$ENV_FILE" ]]; then
  grep -Ev '^(KALSHI_API_KEY_ID|KALSHI_KEY_ID|KALSHI_PRIVATE_KEY_PATH|KALSHI_ENV|KALSHI_ENVIRONMENT|KALSHI_EXECUTION_ENABLED)=' \
    "$ENV_FILE" > "$tmp_env" || true
else
  : > "$tmp_env"
fi
cat >> "$tmp_env" <<VALUES
KALSHI_API_KEY_ID=$kalshi_key_id
KALSHI_PRIVATE_KEY_PATH=$KALSHI_KEY_FILE
KALSHI_ENV=${KALSHI_ENV:-production}
KALSHI_EXECUTION_ENABLED=0
VALUES
mv "$tmp_env" "$ENV_FILE"
chmod 600 "$ENV_FILE"
trap - EXIT

echo "Credentials installed:"
echo "  env=$ENV_FILE"
echo "  kalshi_key=$KALSHI_KEY_FILE"
echo "  execution=disabled"
