#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${POLYMARKET_APP_DIR:?POLYMARKET_APP_DIR is required}"
RUN_ROOT="${PM_V7_RUN_ROOT:?PM_V7_RUN_ROOT is required}"
EXPECTED_SHA="${POLYMARKET_EXPECTED_SHA:?POLYMARKET_EXPECTED_SHA is required}"
STATUS="$RUN_ROOT/control/supervisor_status.json"
CREDENTIALS_FILE="${PM_V7_CREDENTIALS_FILE:-$(dirname "$APP_DIR")/.config/polymarket/v7_credentials.env}"

# Optional market-data credentials live outside Git and outside the run root.
# Parse a strict KEY=VALUE file rather than sourcing shell code.  Only the
# explicit read-only/research adapters' names are accepted, and an existing
# file must be a regular owner-only file owned by this service user.
if [[ -e "$CREDENTIALS_FILE" ]]; then
  python3 - "$CREDENTIALS_FILE" <<'PY'
import os, stat, sys
path=sys.argv[1]
info=os.lstat(path)
assert stat.S_ISREG(info.st_mode), "credentials file is not regular"
assert not stat.S_ISLNK(info.st_mode), "credentials file cannot be a symlink"
assert info.st_uid == os.getuid(), "credentials file owner mismatch"
assert stat.S_IMODE(info.st_mode) & 0o077 == 0, "credentials file permissions are unsafe"
assert info.st_size <= 65536, "credentials file is too large"
allowed={
    "PM_V7_SPORTRADAR_API_KEY", "PM_V7_SPORTRADAR_ACCESS_LEVEL",
    "PM_V7_LIMITLESS_API_KEY", "PM_V7_LIMITLESS_ACCOUNT_ADDRESS",
    "PM_V7_KALSHI_KEY_ID", "PM_V7_KALSHI_PRIVATE_KEY_PATH",
}
for number, raw in enumerate(open(path, encoding="utf-8"), 1):
    line=raw.rstrip("\n")
    if not line or line.startswith("#"):
        continue
    key, separator, value=line.partition("=")
    assert separator and key in allowed and value, f"invalid credentials entry at line {number}"
PY
  while IFS= read -r credential_line || [[ -n "$credential_line" ]]; do
    [[ -z "$credential_line" || "$credential_line" == \#* ]] && continue
    credential_name="${credential_line%%=*}"
    credential_value="${credential_line#*=}"
    case "$credential_name" in
      PM_V7_SPORTRADAR_API_KEY|PM_V7_SPORTRADAR_ACCESS_LEVEL|PM_V7_LIMITLESS_API_KEY|PM_V7_LIMITLESS_ACCOUNT_ADDRESS|PM_V7_KALSHI_KEY_ID|PM_V7_KALSHI_PRIVATE_KEY_PATH)
        export "$credential_name=$credential_value"
        ;;
      *) echo "unexpected credentials key" >&2; exit 78 ;;
    esac
  done < "$CREDENTIALS_FILE"
fi

[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "exact 40-character SHA required" >&2; exit 78; }
[[ "$(git -C "$APP_DIR" rev-parse HEAD)" == "$EXPECTED_SHA" ]] || { echo "checkout SHA drift" >&2; exit 78; }

# Native service managers must not turn an explicitly quarantined state into an
# unbounded restart loop.  Exit successfully until an operator reconciles and
# removes the quarantine status through the controlled deployment path.
if [[ -f "$STATUS" ]]; then
  state="$(python3 - "$STATUS" <<'PY'
import json,sys
try: print(json.load(open(sys.argv[1],encoding='utf-8')).get('state',''))
except Exception: print('')
PY
)"
  case "$state" in
    quarantined|restart_budget_exhausted) exit 0 ;;
  esac
fi

exec python3 "$APP_DIR/ops/v7_runtime_supervisor.py" \
  --repository-root "$APP_DIR" \
  --run-root "$RUN_ROOT" \
  --policy "$APP_DIR/config/v7_runtime_supervision.json" \
  --expected-sha "$EXPECTED_SHA"
