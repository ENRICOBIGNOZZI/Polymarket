#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${POLYMARKET_APP_DIR:-$HOME/polymarket}"
DEPLOY_REF="${POLYMARKET_DEPLOY_REF:-paper-validated}"
MAIN_REF="${POLYMARKET_MAIN_REF:-main}"
EXPECTED_SHA="${POLYMARKET_EXPECTED_SHA:-${EXPECTED_VALIDATED_SHA:-}}"
CACHE_DIR="${POLYMARKET_DEPLOY_CACHE:-$HOME/.cache/polymarket-v7-deploy}"
STATE_DIR="${POLYMARKET_STATE_DIR:-$HOME/.config/polymarket}"
LOCK_DIR="$CACHE_DIR/deploy.lock"
STATUS_FILE="$STATE_DIR/v7_deploy_status.env"
HEALTH_ATTEMPTS="${POLYMARKET_RUNTIME_HEALTH_ATTEMPTS:-120}"

log(){ printf '[v7-deploy] %s\n' "$*"; }
fail(){ printf '[v7-deploy] ERROR: %s\n' "$*" >&2; exit 1; }

[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "POLYMARKET_EXPECTED_SHA must be the exact validated 40-char SHA"
[[ "$DEPLOY_REF" == "paper-validated" ]] || fail "V7 deploy ref must remain paper-validated"
[[ -d "$APP_DIR/.git" ]] || fail "$APP_DIR is not a git checkout"
[[ "$HEALTH_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || fail "POLYMARKET_RUNTIME_HEALTH_ATTEMPTS must be positive"

mkdir -p "$CACHE_DIR" "$STATE_DIR"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  fail "another deployment owns $LOCK_DIR"
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

manifest_meta(){
  python3 - "$1/config/live_champion.json" <<'PY'
import json,sys
from pathlib import Path, PurePosixPath
p=Path(sys.argv[1]); m=json.loads(p.read_text(encoding='utf-8'))
if m.get('enabled') is not True: raise SystemExit('champion disabled')
if m.get('version') != 7: raise SystemExit('enabled champion is not V7')
if m.get('paper_only') is not True or m.get('authenticated_execution') is not False:
    raise SystemExit('unsafe execution boundary')
if m.get('deployment_ref') != 'paper-validated': raise SystemExit('wrong deployment_ref')
vals=[]
for key,prefix in [('loop','scripts/'),('config','config/'),('run_root','runs/')]:
    raw=str(m.get(key) or ''); q=PurePosixPath(raw)
    if not raw.startswith(prefix) or q.is_absolute() or '..' in q.parts: raise SystemExit(f'unsafe {key}')
    vals.append(raw)
print('\t'.join(vals))
PY
}

compose(){
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose "$@"
  else
    return 127
  fi
}

stop_platform_runtime(){
  case "$(uname -s)" in
    Linux)
      local sudo_cmd=()
      [[ "$(id -u)" == "0" ]] || sudo_cmd=(sudo -n)
      for unit in polymarket-v7-paper.service polymarket-paper.service polymarket-paper-latest.service polymarket-v6-paper.service; do
        "${sudo_cmd[@]}" systemctl stop "$unit" >/dev/null 2>&1 || true
      done
      ;;
    Darwin)
      local domain="gui/$(id -u)"
      for label in com.enrico.polymarket.v7.paper com.enrico.polymarket.paper com.enrico.polymarket.v6.paper; do
        launchctl bootout "$domain/$label" >/dev/null 2>&1 || true
      done
      ;;
    *) fail "unsupported server OS: $(uname -s)" ;;
  esac
}

assert_no_legacy_writer(){
  local patterns=('scripts/paper_v3_loop.sh' 'scripts/paper_v4_loop.sh' 'scripts/paper_v5_loop.sh' 'scripts/paper_v6_loop.sh' 'scripts/paper_latest_loop.sh')
  local pattern hits
  for pattern in "${patterns[@]}"; do
    hits="$(pgrep -af "$pattern" 2>/dev/null || true)"
    [[ -z "$hits" ]] || fail "legacy/unowned PAPER writer still alive for $pattern: $hits"
  done
}

start_platform_runtime(){
  local loop_rel="$1" config_rel="$2" run_root_rel="$3"
  mkdir -p "$APP_DIR/$run_root_rel"
  case "$(uname -s)" in
    Linux)
      local sudo_cmd=() unit=/etc/systemd/system/polymarket-v7-paper.service tmp
      [[ "$(id -u)" == "0" ]] || sudo_cmd=(sudo -n)
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
ExecStart=/usr/bin/env bash $APP_DIR/$loop_rel $APP_DIR/$config_rel $APP_DIR/$run_root_rel
Restart=on-failure
RestartSec=5
KillMode=control-group
TimeoutStopSec=45

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
      mkdir -p "$HOME/Library/LaunchAgents" "$APP_DIR/$run_root_rel"
      python3 - "$plist" "$APP_DIR" "$loop_rel" "$config_rel" "$run_root_rel" <<'PY'
import plistlib,sys
from pathlib import Path
plist,root,loop,config,run_root=sys.argv[1:]
data={
 'Label':'com.enrico.polymarket.v7.paper',
 'ProgramArguments':['/bin/bash',str(Path(root)/loop),str(Path(root)/config),str(Path(root)/run_root)],
 'WorkingDirectory':root,
 'RunAtLoad':True,
 'KeepAlive':True,
 'ProcessType':'Background',
 'EnvironmentVariables':{'POLYMARKET_PAPER_ONLY':'1','POLYMARKET_AUTHENTICATED_EXECUTION':'0'},
 'StandardOutPath':str(Path(root)/run_root/'launchd.out.log'),
 'StandardErrorPath':str(Path(root)/run_root/'launchd.err.log'),
}
with open(plist,'wb') as f: plistlib.dump(data,f,sort_keys=True)
PY
      launchctl bootout "$domain/com.enrico.polymarket.v7.paper" >/dev/null 2>&1 || true
      launchctl bootstrap "$domain" "$plist"
      launchctl kickstart -k "$domain/com.enrico.polymarket.v7.paper"
      ;;
  esac
}

start_monitoring(){
  command -v docker >/dev/null 2>&1 || fail "Docker is required for canonical V7 Prometheus/Grafana"
  compose -f "$APP_DIR/docker-compose.monitoring.yml" up -d || fail "failed to start V7 monitoring stack"
}

wait_runtime_contract(){
  local i
  for ((i=0;i<HEALTH_ATTEMPTS;i++)); do
    if python3 "$APP_DIR/scripts/runtime_contract_health.py" --manifest "$APP_DIR/config/live_champion.json" --repository-root "$APP_DIR" --max-age-seconds 180 >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

wait_full_health(){
  local i
  for ((i=0;i<HEALTH_ATTEMPTS;i++)); do
    git -C "$APP_DIR" fetch origin "$MAIN_REF" "$DEPLOY_REF" >/dev/null 2>&1 || true
    if python3 "$APP_DIR/scripts/v7_server_health.py" --repository-root "$APP_DIR" --expected-sha "$EXPECTED_SHA" --max-age-seconds 180 --require-monitoring >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

cd "$APP_DIR"
git fetch origin "$MAIN_REF" "$DEPLOY_REF"
VALIDATED_SHA="$(git rev-parse "origin/$DEPLOY_REF")"
MAIN_SHA="$(git rev-parse "origin/$MAIN_REF")"
[[ "$VALIDATED_SHA" == "$EXPECTED_SHA" ]] || fail "$DEPLOY_REF moved: $VALIDATED_SHA != expected $EXPECTED_SHA"
git merge-base --is-ancestor "$EXPECTED_SHA" "$MAIN_SHA" || fail "$EXPECTED_SHA is not an ancestor of origin/$MAIN_REF"

candidate="$(mktemp -d "$CACHE_DIR/candidate.${EXPECTED_SHA:0:12}.XXXXXX")"
rmdir "$candidate"
git worktree add --detach "$candidate" "$EXPECTED_SHA" >/dev/null
IFS=$'\t' read -r LOOP_REL CONFIG_REL RUN_ROOT_REL <<<"$(manifest_meta "$candidate")"
[[ -f "$candidate/$LOOP_REL" && -f "$candidate/$CONFIG_REL" ]] || fail "selected V7 loop/config is absent at exact SHA"

log "Validating exact V7 candidate $EXPECTED_SHA before touching the active checkout"
python3 "$candidate/scripts/v7_server_health.py" --repository-root "$candidate" --expected-sha "$EXPECTED_SHA" --preflight-only
python3 -m py_compile "$candidate/monitoring/v7_exporter.py" "$candidate/scripts/v7_server_health.py" "$candidate/scripts/runtime_contract_health.py"
bash -n "$candidate/$LOOP_REL" "$candidate/ops/update_server.sh" "$candidate/ops/update_server_macos.sh"
python3 -m json.tool "$candidate/config/live_champion.json" >/dev/null
python3 -m json.tool "$candidate/monitoring/grafana/dashboards/polymarket-v7.json" >/dev/null
(
  cd "$candidate"
  cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
  cmake --build build --parallel "${POLYMARKET_BUILD_JOBS:-2}"
  ctest --test-dir build --output-on-failure
  python3 -m unittest tests/test_v7_cutover_primitives.py tests/test_runtime_contract_health.py tests/test_no_legacy_runtime.py -v
)

OLD_SHA="$(git rev-parse HEAD)"
if [[ "$OLD_SHA" == "$EXPECTED_SHA" ]]; then
  if python3 scripts/v7_server_health.py --repository-root "$APP_DIR" --expected-sha "$EXPECTED_SHA" --max-age-seconds 180 --require-monitoring >/dev/null 2>&1; then
    write_status healthy_already_current
    log "Validated V7 SHA already deployed and healthy"
    exit 0
  fi
  log "Exact V7 SHA is current but unhealthy; performing fail-closed service repair"
fi

stop_platform_runtime
assert_no_legacy_writer
compose -f "$APP_DIR/docker-compose.monitoring.yml" down >/dev/null 2>&1 || true

git checkout --detach "$EXPECTED_SHA"
git reset --hard "$EXPECTED_SHA"
IFS=$'\t' read -r LOOP_REL CONFIG_REL RUN_ROOT_REL <<<"$(manifest_meta "$APP_DIR")"
start_platform_runtime "$LOOP_REL" "$CONFIG_REL" "$RUN_ROOT_REL"
if ! wait_runtime_contract; then
  stop_platform_runtime
  write_status failed_runtime_contract "runtime did not become healthy; V7 left stopped"
  fail "V7 runtime contract did not become healthy; fail-closed with no PAPER writer"
fi

start_monitoring
if ! wait_full_health; then
  stop_platform_runtime
  compose -f "$APP_DIR/docker-compose.monitoring.yml" down >/dev/null 2>&1 || true
  write_status failed_full_health "runtime/monitoring exact-SHA health failed; V7 left stopped"
  fail "full V7 server health failed; fail-closed with no PAPER writer"
fi

write_status deployed_healthy
log "Deployed exact V7 PAPER SHA $EXPECTED_SHA with single-writer runtime and V7 monitoring"
