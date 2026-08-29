#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${POLYMARKET_APP_DIR:-$HOME/polymarket}"
EXPECTED_SHA="${EXPECTED_VALIDATED_SHA:-${POLYMARKET_EXPECTED_SHA:-}}"
DEPLOY_REF="${POLYMARKET_DEPLOY_REF:-paper-validated}"
MAIN_REF="${POLYMARKET_MAIN_REF:-main}"
CACHE_DIR="${POLYMARKET_DEPLOY_CACHE:-$HOME/.cache/polymarket-v7-deploy}"
STATE_DIR="${POLYMARKET_STATE_DIR:-$HOME/.config/polymarket}"
LOCK_DIR="$CACHE_DIR/update-v7.lock"
STATUS_FILE="$STATE_DIR/v7_deploy_status.env"
HEALTH_ATTEMPTS="${POLYMARKET_RUNTIME_HEALTH_ATTEMPTS:-120}"

log(){ printf '[v7-deploy] %s\n' "$*"; }
fail(){ printf '[v7-deploy] ERROR: %s\n' "$*" >&2; exit 1; }

[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "EXPECTED_VALIDATED_SHA must be the exact 40-char validated SHA"
[[ "$DEPLOY_REF" == "paper-validated" ]] || fail "V7 deploy ref must remain paper-validated"
[[ "$MAIN_REF" == "main" ]] || fail "V7 canonical integration ref must remain main"
[[ "$HEALTH_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || fail "POLYMARKET_RUNTIME_HEALTH_ATTEMPTS must be positive"
[[ -d "$APP_DIR/.git" ]] || fail "$APP_DIR is not a git checkout"

mkdir -p "$CACHE_DIR" "$STATE_DIR"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  fail "another V7 deployment owns $LOCK_DIR"
fi
candidate=""
cleanup(){
  if [[ -n "$candidate" && -d "$candidate" ]]; then
    git -C "$APP_DIR" worktree remove --force "$candidate" >/dev/null 2>&1 || true
  fi
  rmdir "$LOCK_DIR" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

write_status(){
  local state="$1" detail="${2:-}"
  local tmp="$STATUS_FILE.tmp.$$"
  {
    printf 'timestamp=%s\n' "$(date +%s)"
    printf 'state=%s\n' "$state"
    printf 'expected_sha=%s\n' "$EXPECTED_SHA"
    printf 'server_head=%s\n' "$(git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || echo missing)"
    printf 'detail=%s\n' "$detail"
  } > "$tmp"
  mv "$tmp" "$STATUS_FILE"
}

monitoring_contract(){
  local root="$1"
  python3 - "$root" <<'PY'
import json,sys
from pathlib import Path, PurePosixPath
root=Path(sys.argv[1])
manifest=root/'monitoring/v7_monitoring_manifest.json'
if not manifest.is_file():
    raise SystemExit('missing V7 monitoring manifest')
m=json.loads(manifest.read_text(encoding='utf-8'))
if m.get('schema') != 'polymarket_v7_monitoring_manifest_v1':
    raise SystemExit('invalid V7 monitoring schema')
if m.get('version') != 7 or m.get('paper_only') is not True or m.get('authenticated_execution') is not False:
    raise SystemExit('unsafe V7 monitoring boundary')
for rel in ('monitoring/exporter_v7.py','monitoring/v7_ledger_metrics.py','monitoring/grafana/dashboards/polymarket-v7.json'):
    if not (root/rel).is_file():
        raise SystemExit(f'missing V7 monitoring asset: {rel}')
graf=m.get('grafana') if isinstance(m.get('grafana'),dict) else {}
uid=str(graf.get('dashboard_uid') or '')
if not uid:
    raise SystemExit('missing V7 dashboard uid')
for key in ('dashboard_file','datasource_file','provider_file'):
    rel=str(graf.get(key) or ''); p=PurePosixPath(rel)
    if not rel or p.is_absolute() or '..' in p.parts or not (root/rel).is_file():
        raise SystemExit(f'invalid V7 monitoring path: {key}')
print(uid)
PY
}

assert_no_legacy_writer(){
  local pattern hits
  for pattern in 'scripts/paper_v3_loop.sh' 'scripts/paper_v4_loop.sh' 'scripts/paper_v5_loop.sh' 'scripts/paper_v6_loop.sh' 'scripts/paper_latest_loop.sh'; do
    hits="$(pgrep -af "$pattern" 2>/dev/null || true)"
    [[ -z "$hits" ]] || fail "legacy/unowned PAPER writer is still alive for $pattern: $hits"
  done
}

stop_v7_runtime(){
  case "$(uname -s)" in
    Linux)
      local sudo_cmd=(); [[ "$(id -u)" == "0" ]] || sudo_cmd=(sudo -n)
      "${sudo_cmd[@]}" systemctl stop polymarket-v7-paper.service >/dev/null 2>&1 || true
      ;;
    Darwin)
      launchctl bootout "gui/$(id -u)/com.enrico.polymarket.v7.paper" >/dev/null 2>&1 || true
      ;;
  esac
}

start_v7_runtime(){
  local loop_rel="$1" config_rel="$2" run_rel="$3"
  mkdir -p "$APP_DIR/$run_rel"
  case "$(uname -s)" in
    Linux)
      local sudo_cmd=() unit tmp
      [[ "$(id -u)" == "0" ]] || sudo_cmd=(sudo -n)
      unit=/etc/systemd/system/polymarket-v7-paper.service
      tmp="$(mktemp)"
      cat >"$tmp" <<EOF
[Unit]
Description=Polymarket canonical V7 PAPER runtime
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=$APP_DIR
Environment=POLYMARKET_PAPER_ONLY=1
Environment=POLYMARKET_AUTHENTICATED_EXECUTION=0
ExecStart=/usr/bin/env bash $APP_DIR/$loop_rel $APP_DIR/$config_rel $APP_DIR/$run_rel
Restart=on-failure
RestartSec=5
KillMode=control-group
TimeoutStopSec=45
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
      "${sudo_cmd[@]}" install -m 0644 "$tmp" "$unit"
      rm -f "$tmp"
      "${sudo_cmd[@]}" systemctl daemon-reload
      "${sudo_cmd[@]}" systemctl enable --now polymarket-v7-paper.service
      ;;
    Darwin)
      local plist="$HOME/Library/LaunchAgents/com.enrico.polymarket.v7.paper.plist" domain="gui/$(id -u)"
      mkdir -p "$HOME/Library/LaunchAgents"
      python3 - "$plist" "$APP_DIR" "$loop_rel" "$config_rel" "$run_rel" <<'PY'
import plistlib,sys
from pathlib import Path
plist,root,loop,config,run_rel=sys.argv[1:]
data={
 'Label':'com.enrico.polymarket.v7.paper',
 'ProgramArguments':['/bin/bash',str(Path(root)/loop),str(Path(root)/config),str(Path(root)/run_rel)],
 'WorkingDirectory':root,
 'RunAtLoad':True,
 'KeepAlive':True,
 'ProcessType':'Background',
 'EnvironmentVariables':{'POLYMARKET_PAPER_ONLY':'1','POLYMARKET_AUTHENTICATED_EXECUTION':'0'},
 'StandardOutPath':str(Path(root)/run_rel/'launchd.out.log'),
 'StandardErrorPath':str(Path(root)/run_rel/'launchd.err.log'),
}
with open(plist,'wb') as handle: plistlib.dump(data,handle,sort_keys=True)
PY
      launchctl bootout "$domain/com.enrico.polymarket.v7.paper" >/dev/null 2>&1 || true
      launchctl bootstrap "$domain" "$plist"
      launchctl kickstart -k "$domain/com.enrico.polymarket.v7.paper"
      ;;
    *) fail "unsupported server OS: $(uname -s)" ;;
  esac
}

apply_monitoring_config(){
  case "$(uname -s)" in
    Darwin)
      [[ -x "$APP_DIR/ops/apply_v7_monitoring_config_macos.sh" ]] || fail "V7 macOS monitoring installer is missing"
      POLYMARKET_APP_DIR="$APP_DIR" bash "$APP_DIR/ops/apply_v7_monitoring_config_macos.sh" >/dev/null
      ;;
    Linux)
      if [[ -f "$APP_DIR/docker-compose.monitoring.yml" ]] && command -v docker >/dev/null 2>&1; then
        docker compose -f "$APP_DIR/docker-compose.monitoring.yml" up -d >/dev/null
      fi
      ;;
  esac
}

stop_monitoring_if_owned(){
  if [[ "$(uname -s)" == "Linux" && -f "$APP_DIR/docker-compose.monitoring.yml" ]] && command -v docker >/dev/null 2>&1; then
    docker compose -f "$APP_DIR/docker-compose.monitoring.yml" down >/dev/null 2>&1 || true
  fi
}

runtime_health(){
  local run_rel="$1" uid="$2" status="$APP_DIR/$run_rel/execution/runtime_status.json" supervisor="$APP_DIR/$run_rel/v7_supervisor.json"
  python3 - "$status" "$supervisor" <<'PY'
import json,sys,time
from pathlib import Path
status_path,supervisor_path=map(Path,sys.argv[1:])
if not status_path.is_file() or not supervisor_path.is_file(): raise SystemExit(1)
status=json.loads(status_path.read_text(encoding='utf-8'))
sup=json.loads(supervisor_path.read_text(encoding='utf-8'))
now=int(time.time())
assert status.get('version') == 7
assert status.get('paper_only') is True
assert status.get('authenticated_execution') is False
assert float(status.get('drawdown',1.0)) <= 0.15 + 1e-12
assert now-int(status.get('timestamp',0)) <= 180
assert sup.get('execution_alive') is True and sup.get('shadow_alive') is True
assert now-int(sup.get('timestamp',0)) <= 60
PY
  curl -fsS http://127.0.0.1:9108/healthz >/dev/null
  local metrics
  metrics="$(curl -fsS http://127.0.0.1:9108/metrics)"
  grep -q '^polymarket_v7_runtime_info 1$' <<<"$metrics"
  grep -q '^polymarket_v7_execution_alive 1$' <<<"$metrics"
  grep -q '^polymarket_v7_shadow_alive 1$' <<<"$metrics"
  curl -fsS http://127.0.0.1:9090/-/ready >/dev/null
  curl -fsS http://127.0.0.1:3000/api/health >/dev/null
  curl -fsS "http://127.0.0.1:3000/api/dashboards/uid/$uid" >/dev/null
}

cd "$APP_DIR"
git fetch --no-tags origin "$MAIN_REF" "$DEPLOY_REF"
MAIN_SHA="$(git rev-parse "origin/$MAIN_REF")"
VALIDATED_SHA="$(git rev-parse "origin/$DEPLOY_REF")"
[[ "$MAIN_SHA" == "$EXPECTED_SHA" ]] || fail "origin/main $MAIN_SHA != exact validated SHA $EXPECTED_SHA"
[[ "$VALIDATED_SHA" == "$EXPECTED_SHA" ]] || fail "origin/paper-validated $VALIDATED_SHA != exact validated SHA $EXPECTED_SHA"

candidate="$(mktemp -d "$CACHE_DIR/candidate.${EXPECTED_SHA:0:12}.XXXXXX")"
rmdir "$candidate"
git worktree add --detach "$candidate" "$EXPECTED_SHA" >/dev/null
python3 "$candidate/scripts/v7_cutover_contract.py" --repository-root "$candidate" --expected-head "$EXPECTED_SHA" >/dev/null
DASHBOARD_UID="$(monitoring_contract "$candidate")"

log "Validating exact V7 candidate $EXPECTED_SHA before active-checkout mutation"
(
  cd "$candidate"
  cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
  cmake --build build --parallel "${POLYMARKET_BUILD_JOBS:-2}"
  ctest --test-dir build --output-on-failure
  python3 -m py_compile scripts/v7_cutover_contract.py monitoring/exporter_v7.py monitoring/v7_ledger_metrics.py
  bash -n scripts/paper_v7_loop.sh scripts/paper_v7_execution_loop.sh ops/update_server_v7.sh
  python3 -m json.tool config/live_champion.json >/dev/null
  python3 -m json.tool config/paper_v7.json >/dev/null
  python3 -m json.tool monitoring/v7_monitoring_manifest.json >/dev/null
  python3 -m json.tool monitoring/grafana/dashboards/polymarket-v7.json >/dev/null
)

git worktree remove --force "$candidate" >/dev/null
candidate=""

assert_no_legacy_writer
OLD_SHA="$(git rev-parse HEAD)"
if [[ "$OLD_SHA" != "$EXPECTED_SHA" ]]; then
  [[ -z "$(git status --porcelain --untracked-files=no)" ]] || fail "tracked server checkout is dirty"
  stop_v7_runtime
  git checkout --detach "$EXPECTED_SHA"
  git reset --hard "$EXPECTED_SHA"
fi

python3 scripts/v7_cutover_contract.py --repository-root "$APP_DIR" --expected-head "$EXPECTED_SHA" >/dev/null
read -r LOOP_REL CONFIG_REL RUN_REL < <(python3 - <<'PY'
import json
from pathlib import Path
m=json.loads(Path('config/live_champion.json').read_text(encoding='utf-8'))
print(m['loop'],m['config'],m['run_root'])
PY
)
DASHBOARD_UID="$(monitoring_contract "$APP_DIR")"
apply_monitoring_config
start_v7_runtime "$LOOP_REL" "$CONFIG_REL" "$RUN_REL"

healthy=0
for ((i=0;i<HEALTH_ATTEMPTS;i++)); do
  if runtime_health "$RUN_REL" "$DASHBOARD_UID" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 2
done
if [[ "$healthy" != "1" ]]; then
  stop_v7_runtime
  stop_monitoring_if_owned
  write_status failed_health "V7 runtime/monitoring failed exact-SHA health; runtime left stopped"
  fail "exact-SHA V7 deploy failed health and was stopped fail-closed"
fi

write_status deployed_healthy
printf 'deploy_result=success\n'
printf 'deployed_sha=%s\n' "$EXPECTED_SHA"
printf 'origin_main=%s\n' "$MAIN_SHA"
printf 'paper_validated=%s\n' "$VALIDATED_SHA"
printf 'previous_sha=%s\n' "$OLD_SHA"
printf 'dashboard_uid=%s\n' "$DASHBOARD_UID"
