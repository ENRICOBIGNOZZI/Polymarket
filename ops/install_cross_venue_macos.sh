#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${POLYMARKET_APP_DIR:-$HOME/polymarket}"
STATE_DIR="${POLYMARKET_STATE_DIR:-$HOME/.config/polymarket}"
LOG_DIR="${POLYMARKET_LOG_DIR:-$HOME/Library/Logs/Polymarket}"
CREDENTIALS="${CROSS_VENUE_CREDENTIALS:-$STATE_DIR/venues.env}"
SUPERVISOR_CONFIG="${PORTFOLIO_SUPERVISOR_CONFIG:-$APP_DIR/config/portfolio_supervisor.json}"
CROSS_CONFIG="${CROSS_VENUE_CONFIG:-$APP_DIR/config/cross_venue.json}"
SUPERVISOR_LABEL="com.polymarket.portfolio-supervisor"
CROSS_LABEL="com.polymarket.cross-venue"

log() { printf '[cross-venue-macos] %s\n' "$*"; }
fail() { printf '[cross-venue-macos] ERROR: %s\n' "$*" >&2; exit 1; }
usage() {
  printf 'usage: %s {install|restart|status|stop|start|uninstall}\n' "$0" >&2
  exit 2
}

[[ "$(uname -s)" == "Darwin" ]] || fail "This service installer is for macOS only"
[[ $# -eq 1 ]] || usage
action="$1"

PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"
[[ -n "$PYTHON_BIN" && -x "$PYTHON_BIN" ]] || fail "python3 is not available"

xml_escape() {
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g'
}

private_mode() {
  local path="$1" mode
  [[ -f "$path" ]] || return 1
  mode="$(/usr/bin/stat -f '%Lp' "$path")"
  [[ "$mode" == "600" || "$mode" == "400" ]]
}

write_plist() {
  local label="$1" program="$2" stdout="$3" stderr="$4"
  shift 4
  local tmp
  tmp="$(mktemp)"
  {
    cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$(xml_escape "$label")</string>
  <key>UserName</key><string>$(xml_escape "$USER")</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml_escape "$program")</string>
EOF
    for arg in "$@"; do printf '    <string>%s</string>\n' "$(xml_escape "$arg")"; done
    cat <<EOF
  </array>
  <key>WorkingDirectory</key><string>$(xml_escape "$APP_DIR")</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key><string>$(xml_escape "$HOME")</string>
    <key>PATH</key><string>$(xml_escape "$PATH")</string>
    <key>CROSS_VENUE_CREDENTIALS</key><string>$(xml_escape "$CREDENTIALS")</string>
    <key>CROSS_VENUE_CONFIG</key><string>$(xml_escape "$CROSS_CONFIG")</string>
    <key>PORTFOLIO_SUPERVISOR_CONFIG</key><string>$(xml_escape "$SUPERVISOR_CONFIG")</string>
    <key>CROSS_VENUE_REQUIRE_AUTH</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key><false/>
  </dict>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$(xml_escape "$stdout")</string>
  <key>StandardErrorPath</key><string>$(xml_escape "$stderr")</string>
</dict>
</plist>
EOF
  } > "$tmp"
  /usr/bin/plutil -lint "$tmp" >/dev/null
  sudo /usr/bin/install -o root -g wheel -m 0644 "$tmp" "/Library/LaunchDaemons/$label.plist"
  rm -f "$tmp"
}

wait_for_file() {
  local path="$1" attempts="${2:-50}"
  local i
  for ((i=1; i<=attempts; ++i)); do
    [[ -s "$path" ]] && return 0
    /bin/sleep 0.2
  done
  return 1
}

validate_install() {
  [[ -d "$APP_DIR/.git" ]] || fail "repository checkout not found at $APP_DIR"
  [[ -f "$CREDENTIALS" ]] || fail "credentials file missing: $CREDENTIALS"
  private_mode "$CREDENTIALS" || fail "credentials file must have mode 600 or 400"
  [[ -f "$SUPERVISOR_CONFIG" ]] || fail "supervisor config missing: $SUPERVISOR_CONFIG"
  [[ -f "$CROSS_CONFIG" ]] || fail "cross-venue config missing: $CROSS_CONFIG"
  [[ -x "$APP_DIR/build/prediction_cross_venue_arb" ]] || fail "cross-venue binary is not built"
  [[ -x "$APP_DIR/build/prediction_cross_venue_auth_check" ]] || fail "auth-check binary is not built"
  [[ -x "$APP_DIR/scripts/cross_venue_loop.sh" ]] || fail "cross-venue loop is not executable"
  [[ -f "$APP_DIR/scripts/portfolio_supervisor.py" ]] || fail "portfolio supervisor is missing"

  "$APP_DIR/build/prediction_cross_venue_auth_check" \
    --config "$CROSS_CONFIG" --credentials "$CREDENTIALS" \
    > "$APP_DIR/runs/cross_venue/auth_check.install.json"
}

install_services() {
  validate_install
  mkdir -p "$LOG_DIR" "$APP_DIR/runs/cross_venue" "$APP_DIR/runs/supervisor"

  write_plist "$SUPERVISOR_LABEL" "$PYTHON_BIN" \
    "$LOG_DIR/portfolio-supervisor.out.log" "$LOG_DIR/portfolio-supervisor.err.log" \
    "$APP_DIR/scripts/portfolio_supervisor.py" --config "$SUPERVISOR_CONFIG" --loop

  write_plist "$CROSS_LABEL" /bin/bash \
    "$LOG_DIR/cross-venue.out.log" "$LOG_DIR/cross-venue.err.log" \
    "$APP_DIR/scripts/cross_venue_loop.sh"

  sudo /bin/launchctl bootout "system/$CROSS_LABEL" 2>/dev/null || true
  sudo /bin/launchctl bootout "system/$SUPERVISOR_LABEL" 2>/dev/null || true
  sudo /bin/launchctl bootstrap system "/Library/LaunchDaemons/$SUPERVISOR_LABEL.plist"
  wait_for_file "$APP_DIR/runs/supervisor/capital_limits.json" 50 || \
    fail "portfolio supervisor did not publish a capital gate"
  sudo /bin/launchctl bootstrap system "/Library/LaunchDaemons/$CROSS_LABEL.plist"
  log "installed independent supervisor and cross-venue services"
}

case "$action" in
  install)
    install_services
    ;;
  restart)
    sudo /bin/launchctl kickstart -k "system/$SUPERVISOR_LABEL"
    wait_for_file "$APP_DIR/runs/supervisor/capital_limits.json" 50 || \
      fail "portfolio supervisor did not publish a capital gate"
    sudo /bin/launchctl kickstart -k "system/$CROSS_LABEL"
    ;;
  status)
    for label in "$SUPERVISOR_LABEL" "$CROSS_LABEL"; do
      printf '===== %s =====\n' "$label"
      sudo /bin/launchctl print "system/$label" | sed -n '1,40p'
    done
    printf 'supervisor_status=%s\n' "$APP_DIR/runs/supervisor/runtime_status.json"
    printf 'cross_venue_status=%s\n' "$APP_DIR/runs/cross_venue/runtime_status.json"
    ;;
  stop)
    sudo /bin/launchctl kill SIGTERM "system/$CROSS_LABEL" 2>/dev/null || true
    sudo /bin/launchctl kill SIGTERM "system/$SUPERVISOR_LABEL" 2>/dev/null || true
    ;;
  start)
    sudo /bin/launchctl kickstart "system/$SUPERVISOR_LABEL"
    wait_for_file "$APP_DIR/runs/supervisor/capital_limits.json" 50 || \
      fail "portfolio supervisor did not publish a capital gate"
    sudo /bin/launchctl kickstart "system/$CROSS_LABEL"
    ;;
  uninstall)
    sudo /bin/launchctl bootout "system/$CROSS_LABEL" 2>/dev/null || true
    sudo /bin/launchctl bootout "system/$SUPERVISOR_LABEL" 2>/dev/null || true
    sudo /bin/rm -f "/Library/LaunchDaemons/$CROSS_LABEL.plist" \
      "/Library/LaunchDaemons/$SUPERVISOR_LABEL.plist"
    log "services removed; runtime state and credentials were retained"
    ;;
  *) usage ;;
esac
