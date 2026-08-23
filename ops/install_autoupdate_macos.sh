#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${POLYMARKET_APP_DIR:-$HOME/polymarket}"
LOG_DIR="${POLYMARKET_LOG_DIR:-$HOME/Library/Logs/Polymarket}"
INTERVAL="${POLYMARKET_UPDATE_INTERVAL_SECONDS:-300}"
LABEL="com.polymarket.autoupdate"

[[ "$(uname -s)" == "Darwin" ]] || { echo "macOS only" >&2; exit 1; }
[[ -x "$APP_DIR/ops/update_server_macos.sh" ]] || { echo "missing $APP_DIR/ops/update_server_macos.sh" >&2; exit 1; }
[[ -f "$APP_DIR/.server_bootstrapped_macos" ]] || { echo "run ops/bootstrap_macos.sh first" >&2; exit 1; }
[[ "$INTERVAL" =~ ^[0-9]+$ ]] && (( INTERVAL >= 60 )) || { echo "interval must be >= 60 seconds" >&2; exit 1; }

mkdir -p "$LOG_DIR"
PLIST="$(mktemp)"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>UserName</key><string>$USER</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$APP_DIR/ops/update_server_macos.sh</string>
  </array>
  <key>WorkingDirectory</key><string>$APP_DIR</string>
  <key>StartInterval</key><integer>$INTERVAL</integer>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$LOG_DIR/autoupdate.out.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/autoupdate.err.log</string>
</dict>
</plist>
EOF
/usr/bin/plutil -lint "$PLIST" >/dev/null
sudo install -o root -g wheel -m 0644 "$PLIST" "/Library/LaunchDaemons/$LABEL.plist"
rm -f "$PLIST"
sudo launchctl bootout "system/$LABEL" 2>/dev/null || true
sudo launchctl bootstrap system "/Library/LaunchDaemons/$LABEL.plist"

printf 'installed=%s interval_seconds=%s\n' "$LABEL" "$INTERVAL"
printf 'status: sudo launchctl print system/%s\n' "$LABEL"
printf 'logs:   %s/autoupdate.{out,err}.log\n' "$LOG_DIR"
