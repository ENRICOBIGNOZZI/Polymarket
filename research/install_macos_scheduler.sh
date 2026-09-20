#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${POLYMARKET_LONDON_HOST:-}"
USER_NAME="${POLYMARKET_LONDON_USER:-enrico}"
RESEARCH_ROOT="${PM_V7_RESEARCH_ROOT:-$HOME/polymarket-research/learning}"
PYTHON="${PM_V7_RESEARCH_PYTHON:-python3}"
"$PYTHON" -c 'import numpy, scipy, sklearn, zoneinfo'
[[ -f "$RESEARCH_ROOT/settings.json" && -f "$RESEARCH_ROOT/research_host.json" ]] || { echo "Enroll research host and source roots first" >&2; exit 64; }
DEST="$HOME/Library/LaunchAgents/com.polymarket.v7.research-cycle.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/polymarket-research"
"$PYTHON" - "$ROOT/ops/launchd/com.polymarket.v7.research-cycle.plist.in" "$DEST" "$ROOT" "$HOST" "$USER_NAME" "$HOME" "$RESEARCH_ROOT" "$PYTHON" <<'PY'
import os,sys
from pathlib import Path
from xml.sax.saxutils import escape
src,dst,root,host,user,home,research,python=sys.argv[1:]
s=Path(src).read_text()
for k,v in {'@APP_DIR@':root,'@LONDON_HOST@':host,'@LONDON_USER@':user,'@HOME@':home,'@RESEARCH_ROOT@':research,'@PYTHON@':python}.items(): s=s.replace(k,escape(v))
if '@' in s: raise SystemExit('unrendered scheduler marker')
t=Path(dst+'.tmp'); t.write_text(s); os.chmod(t,0o600); os.replace(t,dst)
PY
plutil -lint "$DEST" >/dev/null
launchctl bootout "gui/$(id -u)/com.polymarket.v7.research-cycle" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$DEST"
echo "research_scheduler_installed=$DEST schedule=midnight_Europe_Zurich safe_catchup=true automatic_promotion=false"
