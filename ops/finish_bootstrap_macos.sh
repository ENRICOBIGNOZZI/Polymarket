#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${POLYMARKET_APP_DIR:-$HOME/polymarket}"
STATE_DIR="${POLYMARKET_STATE_DIR:-$HOME/.config/polymarket}"

log() { printf '[mac-finish] %s\n' "$*"; }
fail() { printf '[mac-finish] ERROR: %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || fail "This repair is for macOS only"
[[ -d "$APP_DIR/.git" ]] || fail "$APP_DIR is not a git checkout"
[[ -f "$APP_DIR/ops/macos_service_control.sh" ]] || fail "service control helper missing"

log "Creating system helper directory"
sudo install -d -o root -g wheel -m 0755 /usr/local/sbin

log "Installing constrained passwordless service control"
sudo install -o root -g wheel -m 0755 \
  "$APP_DIR/ops/macos_service_control.sh" /usr/local/sbin/polymarket-service-control

SUDOERS_TMP="$(mktemp)"
printf '%s ALL=(root) NOPASSWD: /usr/local/sbin/polymarket-service-control *\n' "$USER" > "$SUDOERS_TMP"
sudo install -d -o root -g wheel -m 0755 /etc/sudoers.d
sudo install -o root -g wheel -m 0440 "$SUDOERS_TMP" /etc/sudoers.d/polymarket-deploy
rm -f "$SUDOERS_TMP"
sudo visudo -cf /etc/sudoers.d/polymarket-deploy >/dev/null

touch "$APP_DIR/.server_bootstrapped_macos"

log "Restarting all Polymarket services"
sudo /usr/local/sbin/polymarket-service-control restart

log "Waiting for runtime health"
for _ in {1..45}; do
  if curl -fsS http://127.0.0.1:9108/healthz >/dev/null 2>&1 && \
     curl -fsS http://127.0.0.1:9090/-/ready >/dev/null 2>&1 && \
     curl -fsS http://127.0.0.1:3000/api/health >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

curl -fsS http://127.0.0.1:9108/healthz >/dev/null || fail "exporter health check failed"
curl -fsS http://127.0.0.1:9108/metrics | grep -q '^polymarket_runtime_info' || fail "canonical runtime metrics missing"
curl -fsS http://127.0.0.1:9090/-/ready >/dev/null || fail "Prometheus is not ready"
curl -fsS http://127.0.0.1:3000/api/health >/dev/null || fail "Grafana is not healthy"

log "Runtime health snapshot"
curl -fsS http://127.0.0.1:9108/metrics | grep -E '^polymarket_runtime_(info|equity_usd|pnl_usd|drawdown_ratio|kill_switch|live_units|execution_staleness_seconds|oos_eligible)' || true

log "Bootstrap finalization complete"
printf 'Git head: %s\n' "$(git -C "$APP_DIR" rev-parse HEAD)"
printf 'Grafana:  http://127.0.0.1:3000\n'
printf 'User:     admin\n'
printf 'Password: %s\n' "$STATE_DIR/grafana-admin-password"
printf 'Status:   sudo /usr/local/sbin/polymarket-service-control status\n'
