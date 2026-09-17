#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${POLYMARKET_LONDON_HOST:?POLYMARKET_LONDON_HOST required}"
USER_NAME="${POLYMARKET_LONDON_USER:-enrico}"
DEST="$HOME/Library/LaunchAgents/com.polymarket.v7.research-cycle.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/polymarket-research"
python3 - "$ROOT/ops/launchd/com.polymarket.v7.research-cycle.plist.in" "$DEST" "$ROOT" "$HOST" "$USER_NAME" "$HOME" <<'PY'
import os,sys
from pathlib import Path
src,dst,root,host,user,home=sys.argv[1:]
s=Path(src).read_text()
for k,v in {'@APP_DIR@':root,'@LONDON_HOST@':host,'@LONDON_USER@':user,'@HOME@':home}.items(): s=s.replace(k,v)
if '@' in s: raise SystemExit('unrendered scheduler marker')
t=Path(dst+'.tmp'); t.write_text(s); os.chmod(t,0o600); os.replace(t,dst)
PY
plutil -lint "$DEST" >/dev/null
launchctl bootout "gui/$(id -u)/com.polymarket.v7.research-cycle" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$DEST"
echo "research_scheduler_installed=$DEST cadence_seconds=3600"
