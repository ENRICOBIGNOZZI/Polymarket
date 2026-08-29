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
git merge-base --is-ancestor "$NEW_SHA" "$MAIN_SHA" || fail "$DEPLOY_REF ($NEW_SHA) is not an ancestor of $LOCAL_BRANCH ($MAIN_SHA)"

if [[ "$OLD_SHA" == "$NEW_SHA" ]]; then
  log "Already at validated commit $NEW_SHA; revalidating and repairing services if needed"
else
  log "Updating $OLD_SHA -> validated commit $NEW_SHA"
  git checkout "$LOCAL_BRANCH"
  git reset --hard "$NEW_SHA"
fi

champion_meta="$(python3 - <<'PY'
import json
from pathlib import Path, PurePosixPath
m=json.loads(Path('config/live_champion.json').read_text(encoding='utf-8'))
version=m.get('version')
assert isinstance(version,int) and not isinstance(version,bool) and version>0
values=[str(m.get(k,'')) for k in ('loop','config','run_root')]
for value in values:
    path=PurePosixPath(value)
    assert not path.is_absolute() and '..' not in path.parts
print('\t'.join((str(version),*values)))
PY
)"
IFS=$'\t' read -r VERSION LOOP_REL CONFIG_REL RUN_ROOT_REL <<<"$champion_meta"
RUN_NAME="$(basename "$RUN_ROOT_REL")"
[[ -f "$LOOP_REL" && -f "$CONFIG_REL" ]] || fail "champion files are missing"

log "Building and validating paper-validated V$VERSION"
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel "$(nproc)"
ctest --test-dir build --output-on-failure
python3 -m unittest \
  tests/test_monitoring_exporter.py \
  tests/test_monitoring_latest_exporter.py \
  tests/test_runtime_contract_health.py \
  tests/test_grafana_multi_strategy_contract.py -v
python3 -m py_compile \
  monitoring/exporter.py monitoring/exporter_latest.py scripts/runtime_contract_health.py
bash -n scripts/paper_latest_loop.sh "$LOOP_REL" scripts/monitoring_up.sh scripts/monitoring_down.sh
python3 -m json.tool "$CONFIG_REL" >/dev/null
python3 -m json.tool config/live_champion.json >/dev/null
python3 -m json.tool monitoring/grafana/dashboards/polymarket-multi-strategy.json >/dev/null
sudo docker compose -f docker-compose.monitoring.yml config >/dev/null

log "Migrating systemd services to manifest-selected runtime"
DEPLOY_USER="$(id -un)"
TMP_ENV="$(mktemp)"
cat > "$TMP_ENV" <<EOF
POLYMARKET_APP_DIR=$APP_DIR
POLYMARKET_RUN_NAME=auto
EOF
sudo install -d -m 0755 /etc/polymarket
sudo install -m 0644 "$TMP_ENV" /etc/polymarket/runtime.env
rm -f "$TMP_ENV"

TMP_PAPER="$(mktemp)"
cat > "$TMP_PAPER" <<EOF
[Unit]
Description=Polymarket manifest-selected paper-live engine
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$DEPLOY_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=/etc/polymarket/runtime.env
ExecStart=/usr/bin/env bash $APP_DIR/scripts/paper_latest_loop.sh
Restart=always
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=45
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
sudo install -m 0644 "$TMP_PAPER" /etc/systemd/system/polymarket-paper.service
rm -f "$TMP_PAPER"

TMP_MON="$(mktemp)"
cat > "$TMP_MON" <<EOF
[Unit]
Description=Polymarket Prometheus/Grafana monitoring
After=docker.service polymarket-paper.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$APP_DIR
Environment=POLYMARKET_RUN_NAME=auto
ExecStart=/usr/bin/docker compose -f $APP_DIR/docker-compose.monitoring.yml up -d --force-recreate
ExecStop=/usr/bin/docker compose -f $APP_DIR/docker-compose.monitoring.yml down
TimeoutStartSec=120
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
EOF
sudo install -m 0644 "$TMP_MON" /etc/systemd/system/polymarket-monitoring.service
rm -f "$TMP_MON"
sudo systemctl daemon-reload

log "Restarting V$VERSION paper and monitoring services"
sudo systemctl restart polymarket-paper.service
sudo systemctl restart polymarket-monitoring.service

dashboard_uid="$(python3 - <<'PY'
import json
from pathlib import Path
print(json.loads(Path('config/project_context.json').read_text(encoding='utf-8'))['grafana']['dashboard_uid'])
PY
)"

healthy=0
metrics=""
for _ in {1..90}; do
  if sudo systemctl is-active --quiet polymarket-paper.service && \
     sudo systemctl is-active --quiet polymarket-monitoring.service && \
     curl -fsS http://127.0.0.1:9108/healthz >/dev/null 2>&1 && \
     curl -fsS http://127.0.0.1:9090/-/ready >/dev/null 2>&1 && \
     curl -fsS http://127.0.0.1:3000/api/health >/dev/null 2>&1 && \
     curl -fsS "http://127.0.0.1:3000/api/dashboards/uid/$dashboard_uid" >/dev/null 2>&1; then
    metrics="$(curl -fsS http://127.0.0.1:9108/metrics 2>/dev/null || true)"
    if grep -Eq "^polymarket_runtime_info\\{adapter=\"[^\"]+\",run_root=\"$RUN_NAME\",version=\"v$VERSION\"\\} 1$" <<<"$metrics"; then
      if (( VERSION == 5 )); then
        if python3 scripts/v5_runtime_readiness.py \
          --run-root "$APP_DIR/$RUN_ROOT_REL" \
          --supervisor-max-age 60 --allocator-max-age 30 \
          --model-output-max-age 120 --startup-grace 600 >/dev/null 2>&1; then
          healthy=1
          break
        fi
      elif python3 scripts/runtime_contract_health.py \
        --manifest config/live_champion.json --repository-root . \
        --max-age-seconds 180 >/dev/null 2>&1; then
        healthy=1
        break
      fi
    fi
  fi
  sleep 2
done
[[ "$healthy" == "1" ]] || fail "V$VERSION runtime did not satisfy the version-neutral health contract"

grep -q '^polymarket_runtime_pnl_usd ' <<<"$metrics"
grep -q '^polymarket_runtime_equity_usd ' <<<"$metrics"
if (( VERSION >= 6 )); then
  grep -q '^polymarket_runtime_contract_present 1$' <<<"$metrics"
fi

printf 'deployed_sha=%s\n' "$NEW_SHA"
printf 'validated_ref=%s\n' "$DEPLOY_REF"
printf 'main_sha=%s\n' "$MAIN_SHA"
printf 'previous_sha=%s\n' "$OLD_SHA"
printf 'champion_version=%s\n' "$VERSION"
printf 'champion_run_root=%s\n' "$RUN_ROOT_REL"
