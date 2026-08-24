#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${POLYMARKET_APP_DIR:-$HOME/polymarket}"
LOCAL_BRANCH="${POLYMARKET_BRANCH:-main}"
DEPLOY_REF="${POLYMARKET_DEPLOY_REF:-paper-validated}"

log() { printf '[deploy] %s\n' "$*"; }
fail() { printf '[deploy] ERROR: %s\n' "$*" >&2; exit 1; }

[[ -d "$APP_DIR/.git" ]] || fail "$APP_DIR is not a git checkout; run ops/bootstrap_server.sh once"
[[ -f "$APP_DIR/.server_bootstrapped" ]] || fail "server bootstrap marker missing; run ops/bootstrap_server.sh once interactively"

cd "$APP_DIR"
OLD_SHA="$(git rev-parse HEAD)"

log "Fetching origin/$LOCAL_BRANCH and validated ref origin/$DEPLOY_REF"
git fetch origin "$LOCAL_BRANCH" "$DEPLOY_REF"
MAIN_SHA="$(git rev-parse "origin/$LOCAL_BRANCH")"
NEW_SHA="$(git rev-parse "origin/$DEPLOY_REF")"
git merge-base --is-ancestor "$NEW_SHA" "$MAIN_SHA" || \
  fail "$DEPLOY_REF ($NEW_SHA) is not an ancestor of $LOCAL_BRANCH ($MAIN_SHA)"

if [[ "$OLD_SHA" == "$NEW_SHA" ]]; then
  log "Already at validated commit $NEW_SHA"
else
  log "Updating $OLD_SHA -> validated commit $NEW_SHA"
  git checkout "$LOCAL_BRANCH"
  git reset --hard "$NEW_SHA"
fi

log "Building and validating paper-validated"
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel "$(nproc)"
ctest --test-dir build --output-on-failure
python3 -m unittest tests/test_monitoring_exporter.py tests/test_monitoring_v4_exporter.py tests/test_monitoring_latest_exporter.py -v
python3 -m py_compile monitoring/exporter.py monitoring/exporter_v4.py monitoring/exporter_latest.py \
  scripts/build_v4_intents.py scripts/merge_v4_intents.py scripts/walk_forward_v4.py scripts/tiny_live_pilot.py
bash -n scripts/paper_v4_once.sh scripts/paper_v4_loop.sh scripts/monitoring_up.sh scripts/monitoring_down.sh
sudo docker compose -f docker-compose.monitoring.yml config >/dev/null

log "Restarting services"
sudo systemctl restart polymarket-paper.service
sudo systemctl restart polymarket-monitoring.service

sleep 3
sudo systemctl is-active --quiet polymarket-paper.service
sudo systemctl is-active --quiet polymarket-monitoring.service
curl -fsS http://127.0.0.1:9108/healthz >/dev/null
curl -fsS http://127.0.0.1:9108/metrics | grep -q '^polymarket_runtime_info'

printf 'deployed_sha=%s\n' "$NEW_SHA"
printf 'validated_ref=%s\n' "$DEPLOY_REF"
printf 'main_sha=%s\n' "$MAIN_SHA"
printf 'previous_sha=%s\n' "$OLD_SHA"
