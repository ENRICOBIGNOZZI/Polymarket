#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${POLYMARKET_APP_DIR:-$HOME/polymarket}"
EXPECTED_SHA="${EXPECTED_DEPLOY_SHA:-${POLYMARKET_EXPECTED_SHA:-}}"
DEPLOY_REF="${POLYMARKET_DEPLOY_REF:-main}"
CACHE_DIR="${POLYMARKET_DEPLOY_CACHE:-$HOME/.cache/polymarket-v7-deploy}"
STATE_DIR="${POLYMARKET_STATE_DIR:-$HOME/.config/polymarket}"
LOCK_DIR="$CACHE_DIR/update-v7.lock"
STATUS_FILE="$STATE_DIR/v7_deploy_status.env"
# External Fair must observe an exact 5-minute Chainlink boundary causally.
# A cutover immediately after a boundary therefore needs one full window plus
# bootstrap slack before the fail-closed health gate can judge the runtime.
HEALTH_ATTEMPTS="${POLYMARKET_RUNTIME_HEALTH_ATTEMPTS:-390}"
DRAIN_ATTEMPTS="${POLYMARKET_RUNTIME_DRAIN_ATTEMPTS:-50}"
POSITION_DRAIN_ATTEMPTS="${POLYMARKET_POSITION_DRAIN_ATTEMPTS:-1200}"
LOCK_STALE_SECONDS="${POLYMARKET_DEPLOY_LOCK_STALE_SECONDS:-7200}"
LOCK_ORPHAN_GRACE_SECONDS="${POLYMARKET_DEPLOY_ORPHAN_GRACE_SECONDS:-300}"
LOCK_NONCE="${EXPECTED_SHA}.$$.$(date +%s)"

log(){ printf '[v7-deploy] %s\n' "$*"; }
fail(){ printf '[v7-deploy] ERROR: %s\n' "$*" >&2; exit 1; }

[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "EXPECTED_DEPLOY_SHA must be the exact 40-char approved SHA"
[[ "$DEPLOY_REF" == "main" ]] || fail "V7 deploy ref must remain main"
[[ "$HEALTH_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || fail "POLYMARKET_RUNTIME_HEALTH_ATTEMPTS must be positive"
[[ "$DRAIN_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || fail "POLYMARKET_RUNTIME_DRAIN_ATTEMPTS must be positive"
[[ "$POSITION_DRAIN_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || fail "POLYMARKET_POSITION_DRAIN_ATTEMPTS must be positive"
[[ "$LOCK_STALE_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail "POLYMARKET_DEPLOY_LOCK_STALE_SECONDS must be positive"
[[ "$LOCK_ORPHAN_GRACE_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail "POLYMARKET_DEPLOY_ORPHAN_GRACE_SECONDS must be positive"
[[ -d "$APP_DIR/.git" ]] || fail "$APP_DIR is not a git checkout"

mkdir -p "$CACHE_DIR" "$STATE_DIR"
recover_orphaned_live_deploy(){
  local owner_pid="$1" started_at="" nonce="" owner_sha="" nonce_started="" age_seconds=""
  local process_meta="" parent_pid="" command_line="" runtime_path="" status_path="" quarantine=""
  started_at="$(cat "$LOCK_DIR/started_at" 2>/dev/null || true)"
  nonce="$(cat "$LOCK_DIR/nonce" 2>/dev/null || true)"
  [[ "$started_at" =~ ^[0-9]+$ ]] || { log "Orphan recovery rejected: invalid started_at"; return 1; }
  [[ "$nonce" =~ ^([0-9a-f]{40})\.([1-9][0-9]*)\.([0-9]+)$ ]] ||
    { log "Orphan recovery rejected: invalid nonce"; return 1; }
  owner_sha="${BASH_REMATCH[1]}"
  [[ "${BASH_REMATCH[2]}" == "$owner_pid" ]] ||
    { log "Orphan recovery rejected: nonce PID mismatch"; return 1; }
  nonce_started="${BASH_REMATCH[3]}"
  (( nonce_started >= started_at - 5 && nonce_started <= started_at + 5 )) ||
    { log "Orphan recovery rejected: nonce timestamp mismatch"; return 1; }
  age_seconds="$(( $(date +%s) - started_at ))"
  (( age_seconds >= LOCK_ORPHAN_GRACE_SECONDS )) ||
    { log "Orphan recovery rejected: grace period active"; return 1; }
  git -C "$APP_DIR" merge-base --is-ancestor "$owner_sha" "$EXPECTED_SHA" >/dev/null 2>&1 ||
    { log "Orphan recovery rejected: owner SHA is not target ancestor"; return 1; }
  process_meta="$(ps -p "$owner_pid" -o ppid=,command= 2>/dev/null || true)"
  read -r parent_pid command_line <<<"$process_meta"
  [[ "$parent_pid" =~ ^[1-9][0-9]*$ && "$command_line" == *bash* ]] ||
    { log "Orphan recovery rejected: lock owner is not a bash updater"; return 1; }
  runtime_path="$APP_DIR/runs/paper_v7_live/control/runtime_status.json"
  status_path="$STATUS_FILE"
  if ! python3 - "$runtime_path" "$status_path" "$owner_sha" "$owner_pid" <<'PY'
import json, os, sys
from pathlib import Path
runtime=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
status={}
for raw in Path(sys.argv[2]).read_text(encoding='utf-8').splitlines():
    if '=' in raw:
        key, value=raw.split('=', 1)
        status[key]=value
owner_sha=sys.argv[3]; owner_pid=int(sys.argv[4])
assert status.get('state') == 'running'
assert status.get('expected_sha') == owner_sha
assert status.get('server_head') == owner_sha
assert status.get('detail') == 'exact V7 SHA started; monitoring health pending'
assert runtime.get('model_sha') == owner_sha
assert runtime.get('paper_only') is True
assert runtime.get('authenticated_execution') is False
assert runtime.get('real_order_submission') is False
runtime_pid=int(runtime.get('pid') or 0)
assert runtime_pid > 0 and runtime_pid != owner_pid
os.kill(runtime_pid, 0)
PY
  then
    log "Orphan recovery rejected: runtime/deploy status proof invalid"
    return 1
  fi
  kill -TERM "$owner_pid" 2>/dev/null || true
  for _ in $(seq 1 50); do
    kill -0 "$owner_pid" 2>/dev/null || break
    sleep 0.1
  done
  if kill -0 "$owner_pid" 2>/dev/null; then
    kill -KILL "$owner_pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$owner_pid" 2>/dev/null || break
      sleep 0.05
    done
  fi
  kill -0 "$owner_pid" 2>/dev/null && { log "Orphan recovery rejected: updater survived bounded termination"; return 1; }
  if [[ ! -e "$LOCK_DIR" ]]; then
    mkdir "$LOCK_DIR" 2>/dev/null || return 1
    log "Recovered verified orphan deployment pid=$owner_pid sha=$owner_sha age=${age_seconds}s"
    return 0
  fi
  [[ "$(cat "$LOCK_DIR/nonce" 2>/dev/null || true)" == "$nonce" ]] || return 1
  find "$LOCK_DIR" -mindepth 1 -maxdepth 1 \
    ! -name owner_pid ! -name started_at ! -name nonce -print -quit | grep -q . && return 1
  quarantine="$CACHE_DIR/update-v7.orphan.$$.${age_seconds}"
  mv "$LOCK_DIR" "$quarantine" 2>/dev/null || return 1
  rm -f "$quarantine/owner_pid" "$quarantine/started_at" "$quarantine/nonce"
  rmdir "$quarantine" || return 1
  mkdir "$LOCK_DIR" 2>/dev/null || return 1
  log "Recovered verified orphan deployment pid=$owner_pid sha=$owner_sha age=${age_seconds}s"
}

acquire_deploy_lock(){
  local owner_pid="" age_seconds="" quarantine="" recovered_live=0
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    owner_pid="$(cat "$LOCK_DIR/owner_pid" 2>/dev/null || true)"
    if [[ "$owner_pid" =~ ^[1-9][0-9]*$ ]] && kill -0 "$owner_pid" 2>/dev/null; then
      if recover_orphaned_live_deploy "$owner_pid"; then
        recovered_live=1
      else
        fail "another live V7 deployment pid=$owner_pid owns $LOCK_DIR"
      fi
    fi
    if [[ "$recovered_live" == 0 ]]; then
      age_seconds="$(python3 - "$LOCK_DIR" <<'PY'
import os,sys,time
try:
    print(max(0, int(time.time() - os.stat(sys.argv[1]).st_mtime)))
except FileNotFoundError:
    print(0)
PY
)"
      [[ "$age_seconds" =~ ^[0-9]+$ ]] || fail "cannot determine V7 deployment lock age"
      if (( age_seconds < LOCK_STALE_SECONDS )); then
        fail "V7 deployment lock owner is unavailable but lease is not stale: age=${age_seconds}s"
      fi
      find "$LOCK_DIR" -mindepth 1 -maxdepth 1 \
        ! -name owner_pid ! -name started_at ! -name nonce -print -quit | grep -q . && \
        fail "expired V7 deployment lock contains unexpected state: $LOCK_DIR"
      quarantine="$CACHE_DIR/update-v7.stale.$$.${age_seconds}"
      mv "$LOCK_DIR" "$quarantine" 2>/dev/null || fail "V7 deployment lock changed during stale-lock recovery"
      rm -f "$quarantine/owner_pid" "$quarantine/started_at" "$quarantine/nonce"
      if ! rmdir "$quarantine"; then
        if [[ ! -e "$LOCK_DIR" ]]; then mv "$quarantine" "$LOCK_DIR" 2>/dev/null || true; fi
        fail "could not retire stale V7 deployment lock"
      fi
      mkdir "$LOCK_DIR" 2>/dev/null || fail "another V7 deployment won stale-lock recovery"
      log "Recovered expired orphan deployment lock age=${age_seconds}s"
    fi
  fi
  umask 077
  printf '%s\n' "$$" > "$LOCK_DIR/owner_pid"
  printf '%s\n' "$(date +%s)" > "$LOCK_DIR/started_at"
  printf '%s\n' "$LOCK_NONCE" > "$LOCK_DIR/nonce"
}
acquire_deploy_lock
candidate=""
cleanup(){
  if [[ -n "$candidate" && -d "$candidate" ]]; then
    git -C "$APP_DIR" worktree remove --force "$candidate" >/dev/null 2>&1 || true
  fi
  if [[ "$(cat "$LOCK_DIR/nonce" 2>/dev/null || true)" == "$LOCK_NONCE" ]]; then
    rm -f "$LOCK_DIR/owner_pid" "$LOCK_DIR/started_at" "$LOCK_DIR/nonce"
    rmdir "$LOCK_DIR" >/dev/null 2>&1 || true
  fi
  clear_cutover_drain
}
trap cleanup EXIT INT TERM

write_status(){
  local state="$1" detail="${2:-}" tmp="$STATUS_FILE.tmp.$$"
  {
    printf 'timestamp=%s\n' "$(date +%s)"
    printf 'state=%s\n' "$state"
    printf 'expected_sha=%s\n' "$EXPECTED_SHA"
    printf 'server_head=%s\n' "$(git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || echo missing)"
    printf 'detail=%s\n' "$detail"
  } > "$tmp"
  mv "$tmp" "$STATUS_FILE"
}

production_run_root(){ printf '%s\n' "$APP_DIR/runs/paper_v7_live"; }

resolve_incumbent_sha(){
  local run_root deployed runtime
  run_root="$(production_run_root)"
  deployed="$run_root/control/deployed_sha"
  runtime="$run_root/control/runtime_status.json"
  if [[ ! -f "$deployed" && ! -f "$runtime" ]]; then
    git -C "$APP_DIR" rev-parse HEAD
    return 0
  fi
  python3 - "$deployed" "$runtime" <<'PY'
import json,re,sys
from pathlib import Path
sha_pattern=re.compile(r'^[0-9a-f]{40}$')
deployed_path,runtime_path=map(Path,sys.argv[1:])
try:
    deployed=deployed_path.read_text(encoding='utf-8').strip()
    runtime=json.loads(runtime_path.read_text(encoding='utf-8'))
except (OSError,json.JSONDecodeError) as exc:
    raise SystemExit(f'incumbent_identity_unreadable:{type(exc).__name__}')
runtime_sha=str(runtime.get('model_sha') or '')
if not sha_pattern.fullmatch(deployed) or runtime_sha != deployed:
    raise SystemExit('incumbent_identity_mismatch')
if runtime.get('paper_only') is not True or runtime.get('authenticated_execution') is not False:
    raise SystemExit('incumbent_safety_mismatch')
if runtime.get('real_order_submission') is not False:
    raise SystemExit('incumbent_real_submission_enabled')
print(deployed)
PY
}

clear_cutover_drain(){
  local path="$(production_run_root)/control/CUTOVER_DRAIN"
  python3 - "$path" "$LOCK_NONCE" <<'PY' 2>/dev/null || true
import json, os, sys
from pathlib import Path
path=Path(sys.argv[1]); nonce=sys.argv[2]
try:
    value=json.loads(path.read_text(encoding='utf-8'))
except (OSError, json.JSONDecodeError):
    raise SystemExit(0)
if value.get('nonce') == nonce:
    path.unlink(missing_ok=True)
PY
}

request_cutover_drain(){
  local current_sha="$1" run_root="$(production_run_root)"
  [[ "$current_sha" != "$EXPECTED_SHA" ]] || return 0
  python3 - "$run_root" "$LOCK_NONCE" "$current_sha" "$EXPECTED_SHA" <<'PY'
import json, os, sys, time
from pathlib import Path
root=Path(sys.argv[1]); nonce,current_sha,target_sha=sys.argv[2:]
control=root/'control'; control.mkdir(parents=True,exist_ok=True)
path=control/'CUTOVER_DRAIN'; temporary=path.with_name(f'{path.name}.tmp.{os.getpid()}')
value={
    'schema':'polymarket_v7_cutover_drain_v1', 'nonce':nonce,
    'paper_only':True, 'authenticated_execution':False, 'real_order_submission':False,
    'current_sha':current_sha, 'target_sha':target_sha, 'requested_at':int(time.time()),
}
temporary.write_text(json.dumps(value,sort_keys=True)+'\n',encoding='utf-8')
os.replace(temporary,path)
PY
  log "Requested fail-closed PAPER position drain from $current_sha to $EXPECTED_SHA"
}

cutover_positions_drained(){
  local run_root="$(production_run_root)"
  python3 - "$run_root" "$LOCK_NONCE" <<'PY'
import json, sys
from pathlib import Path
root=Path(sys.argv[1]); nonce=sys.argv[2]
def read(rel):
    try:
        value=json.loads((root/rel).read_text(encoding='utf-8'))
    except (OSError,json.JSONDecodeError):
        return {}
    return value if isinstance(value,dict) else {}
sentinel=read('control/CUTOVER_DRAIN')
external_status=read('external_fair/paper_router_status.json')
external_state=read('external_fair/paper_router_state.json')
micro_status=read('micro_taker/status.json')
micro_state=read('micro_taker/state.json')
maker_status=read('micro_maker/status.json')
maker_state=read('micro_maker/state.json')
assert sentinel.get('nonce') == nonce and sentinel.get('paper_only') is True
assert external_status.get('paper_only') is True and external_status.get('authenticated_execution') is False
assert micro_status.get('paper_only') is True and micro_status.get('authenticated_execution') is False
assert maker_status.get('paper_only') is True and maker_status.get('authenticated_execution') is False
external_positions=external_state.get('positions'); micro_positions=micro_state.get('positions')
maker_inventory=maker_state.get('inventory')
assert isinstance(external_positions,dict) and isinstance(micro_positions,dict)
assert isinstance(maker_inventory,dict)
# paper_router_state.positions is the counterfactual SHADOW book. It is not
# execution inventory and must never hold an exact-SHA cutover hostage. The
# router status owns the zero-authority execution-position contract; durable
# state is reconciled separately only as counterfactual evidence.
external_open=int(external_status.get('open_positions',-1))
counterfactual_open=sum(
    1 for row in external_positions.values()
    if isinstance(row,dict) and row.get('settled') is not True
)
micro_open=len(micro_positions)
maker_open=0
for row in maker_inventory.values():
    assert isinstance(row,dict)
    yes=float(row.get('yes_shares') or 0.0); no=float(row.get('no_shares') or 0.0)
    assert yes >= 0.0 and no >= 0.0
    maker_open += int(yes > 1e-9) + int(no > 1e-9)
assert external_open >= 0
assert int(external_status.get('counterfactual_open_positions',-1)) == counterfactual_open
assert int(micro_status.get('open_positions',-1)) == micro_open
assert external_open == 0 and micro_open == 0
assert maker_status.get('killed') is not True
# Every supported incumbent is drain-aware. External and micro must already be
# flat; Maker only needs to prove entry is frozen because its durable inventory
# is terminalized immediately afterward by target-SHA code.
assert external_status.get('drain_requested') is True
assert external_status.get('drain_complete') is True
assert external_status.get('order_submission_enabled') is False
assert external_status.get('blocker') == 'CUTOVER_DRAIN'
assert micro_status.get('drain_requested') is True
assert micro_status.get('drain_complete') is True
assert micro_status.get('new_risk_frozen') is True
assert maker_status.get('drain_requested') is True
assert maker_status.get('new_risk_frozen') is True
assert maker_status.get('drain_complete') is (maker_open == 0)
PY
}

wait_for_cutover_drain(){
  local current_sha="$1"
  [[ "$current_sha" != "$EXPECTED_SHA" ]] || return 0
  for _ in $(seq 1 "$POSITION_DRAIN_ATTEMPTS"); do
    if cutover_positions_drained >/dev/null 2>&1; then
      log "PAPER entry drain complete; durable Maker inventory is frozen for target-SHA finalization"
      return 0
    fi
    sleep 1
  done
  fail "PAPER position drain did not reach zero terminally-accounted positions"
}

record_deployed_sha(){
  local run_root="$(production_run_root)" tmp
  mkdir -p "$run_root/control"
  tmp="$run_root/control/deployed_sha.tmp.$$"
  printf '%s\n' "$EXPECTED_SHA" > "$tmp"
  mv "$tmp" "$run_root/control/deployed_sha"
}

record_incumbent_identity(){
  local run_root="$(production_run_root)"
  python3 - "$run_root" "$EXPECTED_SHA" <<'PY'
import json,os,sys,time
from pathlib import Path
root=Path(sys.argv[1]); expected=sys.argv[2]
runtime=json.loads((root/'control/runtime_status.json').read_text(encoding='utf-8'))
required=('config_hash','policy_hash','model_hash','run_id','ledger_id','server_id')
assert runtime.get('model_sha') == expected
assert runtime.get('paper_only') is True
assert runtime.get('authenticated_execution') is False
assert runtime.get('real_order_submission') is False
assert all(str(runtime.get(key) or '').strip() for key in required)
value={
    'schema':'polymarket_v7_incumbent_identity_v1',
    'recorded_at':int(time.time()),
    'paper_only':True,
    'execution_authority':'FROZEN_BLUE_CHAMPION',
    'runtime_sha':expected,
    'config_hash':runtime['config_hash'],
    'policy_hash':runtime['policy_hash'],
    'model_hash':runtime['model_hash'],
    'model_identity_source':runtime.get('model_identity_source'),
    'run_id':runtime['run_id'],
    'ledger_id':runtime['ledger_id'],
    'server_id':runtime['server_id'],
    'verified':True,
}
path=root/'control/incumbent_identity.json'; tmp=path.with_suffix(f'.tmp.{os.getpid()}')
tmp.write_text(json.dumps(value,sort_keys=True)+'\n',encoding='utf-8'); os.replace(tmp,path)
PY
}

production_pid(){
  local status="$(production_run_root)/control/runtime_status.json"
  python3 - "$status" <<'PY' 2>/dev/null || true
import json,sys
from pathlib import Path
p=Path(sys.argv[1])
if p.is_file():
    try:
        pid=int(json.loads(p.read_text()).get('pid') or 0)
        if pid>0: print(pid)
    except Exception:
        pass
PY
}

production_supervisor_pid(){
  local status="$(production_run_root)/control/supervisor_status.json"
  python3 - "$status" <<'PY' 2>/dev/null || true
import json,sys
from pathlib import Path
p=Path(sys.argv[1])
if p.is_file():
    try:
        pid=int(json.loads(p.read_text()).get('supervisor_pid') or 0)
        if pid>0: print(pid)
    except Exception:
        pass
PY
}

force_stop_production_tree(){
  local root_pid="$1"
  python3 - "$root_pid" <<'PY'
import os
import signal
import subprocess
import sys
import time

root = int(sys.argv[1])
try:
    raw = subprocess.check_output(["ps", "-axo", "pid=,ppid="], text=True)
except Exception as exc:
    raise SystemExit(f"cannot inspect V7 process tree: {exc}")

children = {}
for line in raw.splitlines():
    fields = line.split()
    if len(fields) != 2:
        continue
    try:
        pid, ppid = map(int, fields)
    except ValueError:
        continue
    children.setdefault(ppid, []).append(pid)

stack = [root]
seen = set()
order = []
while stack:
    pid = stack.pop()
    if pid in seen:
        continue
    seen.add(pid)
    order.append(pid)
    stack.extend(children.get(pid, ()))

for pid in reversed(order):
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

end = time.monotonic() + 2.0
while time.monotonic() < end:
    alive = []
    for pid in order:
        try:
            os.kill(pid, 0)
            alive.append(pid)
        except ProcessLookupError:
            pass
    if not alive:
        raise SystemExit(0)
    time.sleep(0.05)

for pid in reversed(order):
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
PY
}

stop_production_runtime(){
  local supervisor_pid="$(production_supervisor_pid)" pid="$(production_pid)"
  if [[ "$supervisor_pid" =~ ^[1-9][0-9]*$ ]] && kill -0 "$supervisor_pid" 2>/dev/null; then
    log "Stopping canonical V7 supervisor pid=$supervisor_pid"
    kill -TERM "$supervisor_pid" 2>/dev/null || true
    for _ in $(seq 1 $((DRAIN_ATTEMPTS * 4))); do
      kill -0 "$supervisor_pid" 2>/dev/null || break
      sleep 0.1
    done
    if kill -0 "$supervisor_pid" 2>/dev/null; then
      fail "canonical V7 supervisor pid=$supervisor_pid did not stop cleanly"
    fi
  fi
  if [[ "$pid" =~ ^[1-9][0-9]*$ ]] && kill -0 "$pid" 2>/dev/null; then
    log "Stopping production V7 pid=$pid only"
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 "$DRAIN_ATTEMPTS"); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.1
    done
    if kill -0 "$pid" 2>/dev/null; then
      log "Graceful V7 drain timed out; force-stopping owned process tree rooted at pid=$pid"
      force_stop_production_tree "$pid"
      for _ in $(seq 1 50); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.1
      done
    fi
    if kill -0 "$pid" 2>/dev/null; then
      fail "production V7 pid=$pid survived bounded owned-tree termination"
    fi
  fi
  return 0
}

monitoring_contract(){
  local root="$1"
  python3 - "$root" <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1])
m=json.loads((root/'monitoring/v7_monitoring_manifest.json').read_text())
assert m.get('schema')=='polymarket_v7_monitoring_manifest_v2'
assert m.get('version')==7
assert m.get('paper_only') is True
assert m.get('authenticated_execution') is False
for rel in (
    'monitoring/exporter_v7.py',
    'monitoring/v7_ledger_metrics.py',
    'monitoring/v7_alerts.yml',
    'monitoring/v7_runtime_contract.py',
    'monitoring/v7_retention.py',
    'monitoring/grafana/dashboards/polymarket-v7.json',
    'monitoring/grafana/dashboards/polymarket-v7-external-fair.json',
    'ops/v7_runtime_supervisor.py',
    'ops/v7_service_entrypoint.sh',
    'scripts/v7_rtds_external_fair_monitor.py',
    'scripts/v7_external_fair_paper_router.py',
    'scripts/v7_research_shadow_supervisor.py',
    'scripts/v7_semantic_mapping.py',
    'scripts/v7_sports_collector.py',
    'scripts/v7_cross_platform_collector.py',
    'scripts/v7_osint_mapping_collector.py',
    'config/v7_live_model_scope.json',
    'config/v7_external_inputs.json',
    'config/v7_external_mappings.json',
    'config/v7_external_fair_rule_approvals.json',
    'config/v7_runtime_supervision.json',
    'config/v7_data_retention.json',
):
    assert (root/rel).is_file(), rel
print(m['grafana']['dashboard_uid'])
PY
}

prevalidate_candidate(){
  candidate="$(mktemp -d "$CACHE_DIR/candidate.${EXPECTED_SHA:0:12}.XXXXXX")"
  rmdir "$candidate"
  git -C "$APP_DIR" worktree add --detach "$candidate" "$EXPECTED_SHA" >/dev/null
  log "Validating exact V7 candidate $EXPECTED_SHA before active-checkout mutation"
  (
    cd "$candidate"
    python3 scripts/v7_cutover_contract.py --repository-root . --expected-head "$EXPECTED_SHA" >/dev/null
    monitoring_contract "$candidate" >/dev/null
    local brew_prefix=""
    if command -v brew >/dev/null 2>&1; then brew_prefix="$(brew --prefix)"; fi
    cmake -S . -B build -DCMAKE_BUILD_TYPE=Release ${brew_prefix:+-DCMAKE_PREFIX_PATH="$brew_prefix"}
    cmake --build build --parallel "${POLYMARKET_BUILD_JOBS:-2}"
    ctest --test-dir build --output-on-failure
    python3 -m py_compile \
      scripts/v7_cutover_contract.py \
      scripts/v7_execution_ledger.py \
      scripts/v7_ledger_spool.py \
      scripts/v7_canonical_economics.py \
      scripts/v7_portfolio_guard.py \
      scripts/v7_prepare_cutover_run_root.py \
      scripts/v7_finalize_maker_cutover.py \
      scripts/v7_research_shadow_supervisor.py \
      scripts/v7_semantic_mapping.py \
      scripts/v7_sports_collector.py \
      scripts/v7_cross_platform_collector.py \
      scripts/v7_osint_mapping_collector.py \
      scripts/v7_rtds_external_fair_monitor.py \
      scripts/v7_external_fair_paper_router.py \
      monitoring/exporter_v7.py \
      monitoring/v7_ledger_metrics.py \
      monitoring/v7_runtime_contract.py \
      monitoring/v7_retention.py \
      ops/v7_runtime_supervisor.py
    bash -n scripts/paper_v7_execution_loop.sh ops/update_server_v7.sh ops/v7_service_entrypoint.sh
    python3 -m json.tool config/live_champion.json >/dev/null
    python3 -m json.tool config/paper_v7.json >/dev/null
    python3 -m json.tool monitoring/v7_monitoring_manifest.json >/dev/null
    python3 -m json.tool monitoring/grafana/dashboards/polymarket-v7.json >/dev/null
    python3 -m json.tool config/v7_runtime_supervision.json >/dev/null
    python3 -m json.tool config/v7_data_retention.json >/dev/null
    python3 -m json.tool config/v7_live_model_scope.json >/dev/null
    python3 -m json.tool config/v7_external_inputs.json >/dev/null
    python3 -m json.tool config/v7_external_mappings.json >/dev/null
  )
  # Keep the exact target worktree until cleanup. Cutover marking/finalization
  # must use candidate code, not the older incumbent checkout.
}

build_current_checkout(){
  local brew_prefix=""
  if command -v brew >/dev/null 2>&1; then brew_prefix="$(brew --prefix)"; fi
  log "Recreating active CMake build directory at $APP_DIR/build"
  rm -rf "$APP_DIR/build"
  cmake -S "$APP_DIR" -B "$APP_DIR/build" -DCMAKE_BUILD_TYPE=Release ${brew_prefix:+-DCMAKE_PREFIX_PATH="$brew_prefix"}
  cmake --build "$APP_DIR/build" --parallel "${POLYMARKET_BUILD_JOBS:-2}"
}

start_production_runtime(){
  local run_root="$(production_run_root)" runtime_log log_epoch_line
  runtime_log="$run_root/deploy-runtime.log"
  mkdir -p "$run_root"
  # Only an explicitly authorized exact-SHA deployment clears a supervisor
  # quarantine. Ledger, inventory and KILL evidence remain for reconciliation.
  rm -f "$run_root/control/supervisor_status.json"
  printf '\n=== V7 DEPLOY EPOCH expected_sha=%s started_at=%s ===\n' \
    "$EXPECTED_SHA" "$(date +%s)" >> "$runtime_log"
  log_epoch_line="$(wc -l < "$runtime_log" | tr -d ' ')"
  local pid=""
  if [[ "$(uname -s)" == "Darwin" ]]; then
    local domain="gui/$(id -u)" label="com.polymarket.v7.paper"
    local template="$APP_DIR/ops/launchd/com.polymarket.v7.paper.plist.in"
    local destination="$HOME/Library/LaunchAgents/$label.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    python3 - "$template" "$destination" "$APP_DIR" "$run_root" "$EXPECTED_SHA" <<'PY'
import os,sys
from pathlib import Path
source,destination,app,run_root,sha=map(str,sys.argv[1:])
payload=Path(source).read_text(encoding='utf-8')
for marker,value in (("@APP_DIR@",app),("@RUN_ROOT@",run_root),("@EXPECTED_SHA@",sha)):
    payload=payload.replace(marker,value)
assert "@" not in payload
path=Path(destination); temporary=path.with_name(path.name+f'.tmp.{os.getpid()}')
temporary.write_text(payload,encoding='utf-8'); os.chmod(temporary,0o644); os.replace(temporary,path)
PY
    plutil -lint "$destination" >/dev/null
    launchctl bootout "$domain/$label" >/dev/null 2>&1 || true
    # Recovery tooling uses this same loopback proxy. A verified stale owner
    # must be gone before launchd starts the canonical child, otherwise the
    # address collision kills the whole fail-closed runtime tree.
    stop_stale_monitoring_listener public_https_proxy 19109 "scripts/v7_public_https_proxy.py"
    launchctl bootstrap "$domain" "$destination"
    log "Started production V7 under launchd label=$label"
  else
    nohup env \
      POLYMARKET_APP_DIR="$APP_DIR" \
      PM_V7_RUN_ROOT="$run_root" \
      POLYMARKET_EXPECTED_SHA="$EXPECTED_SHA" \
      PM_V7_EXACT_SHA_CI_GREEN=true \
      PM_TRADE_RECORDER="$APP_DIR/build/polymarket_v7_trade_recorder" \
      bash "$APP_DIR/ops/v7_service_entrypoint.sh" \
      >>"$runtime_log" 2>&1 </dev/null &
    pid=$!
    log "Started production V7 pid=$pid"
  fi
  sleep 2
  if [[ "$(uname -s)" == "Darwin" ]]; then
    launchctl print "gui/$(id -u)/com.polymarket.v7.paper" 2>/dev/null | grep -q 'state = running' || {
      tail -n 80 "$run_root/supervisor-launchd.log" >&2 || true
      fail "launchd production V7 exited during startup"
    }
  else
    kill -0 "$pid" 2>/dev/null || {
      tail -n "+$log_epoch_line" "$runtime_log" >&2 || true
      fail "production V7 exited during startup"
    }
  fi
}

stop_owned_monitoring(){
  local name pidfile pid
  for name in exporter prometheus grafana; do
    pidfile="$STATE_DIR/${name}-v7.pid"
    if [[ -f "$pidfile" ]]; then
      pid="$(cat "$pidfile" 2>/dev/null || true)"
      if [[ "$pid" =~ ^[1-9][0-9]*$ ]] && kill -0 "$pid" 2>/dev/null; then
        kill -TERM "$pid" 2>/dev/null || true
        for _ in $(seq 1 50); do
          kill -0 "$pid" 2>/dev/null || break
          sleep 0.1
        done
        if kill -0 "$pid" 2>/dev/null; then
          kill -KILL "$pid" 2>/dev/null || true
          for _ in $(seq 1 20); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.05
          done
        fi
        if kill -0 "$pid" 2>/dev/null; then
          fail "owned monitoring process $name pid=$pid survived bounded shutdown"
        fi
      fi
      rm -f "$pidfile"
    fi
  done
}

stop_stale_monitoring_listener(){
  local name="$1" port="$2" expected="$3" pid command_line pids
  command -v lsof >/dev/null 2>&1 || return 0
  pids="$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | sort -u || true)"
  for pid in $pids; do
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || continue
    command_line="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    [[ "$command_line" == *"$expected"* ]] || \
      fail "refusing to replace unknown listener name=$name port=$port pid=$pid command=$command_line"
    log "Stopping stale $name listener pid=$pid port=$port before SHA cutover"
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 50); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.1
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || true
      for _ in $(seq 1 20); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.05
      done
    fi
    if kill -0 "$pid" 2>/dev/null; then
      fail "stale monitoring listener $name pid=$pid survived shutdown"
    fi
  done
  return 0
}

start_monitoring(){
  local run_root="$(production_run_root)" monitoring_root="$APP_DIR/runs/monitoring"
  if [[ "$(uname -s)" == "Darwin" ]]; then
    local domain="gui/$(id -u)" label template destination
    for label in exporter prometheus grafana; do
      launchctl bootout "$domain/com.polymarket.v7.$label" >/dev/null 2>&1 || true
    done
    POLYMARKET_APP_DIR="$APP_DIR" POLYMARKET_STATE_DIR="$STATE_DIR" \
      bash "$APP_DIR/ops/apply_v7_monitoring_config_macos.sh" >/dev/null
    if command -v brew >/dev/null 2>&1; then
      brew services stop prometheus >/dev/null 2>&1 || true
      brew services stop grafana >/dev/null 2>&1 || true
    fi
    stop_owned_monitoring
    # PID files can be lost across interrupted deployments. Resolve every
    # canonical port after booting out launchd and replace only a positively
    # identified V7 process. Unknown listeners remain fail-closed.
    stop_stale_monitoring_listener exporter 9108 "$APP_DIR/monitoring/exporter_v7.py"
    stop_stale_monitoring_listener prometheus 9090 "prometheus"
    stop_stale_monitoring_listener grafana 3000 "grafana"
    local prometheus_bin="$(command -v prometheus)" grafana_bin="$(command -v grafana)"
    local grafana_home="${POLYMARKET_GRAFANA_HOME:-}"
    [[ -n "$prometheus_bin" && -n "$grafana_bin" ]] || fail "canonical monitoring binaries unavailable"
    if [[ -z "$grafana_home" ]] && command -v brew >/dev/null 2>&1; then
      grafana_home="$(brew --prefix grafana)/share/grafana"
    fi
    [[ -n "$grafana_home" ]] || grafana_home="/usr/share/grafana"
    mkdir -p "$monitoring_root/prometheus"
    mkdir -p "$monitoring_root/grafana/data" "$monitoring_root/grafana/logs" \
      "$monitoring_root/grafana/plugins"
    python3 - "$APP_DIR" "$run_root" "$STATE_DIR" "$prometheus_bin" "$grafana_bin" "$grafana_home" <<'PY'
import os, sys
from pathlib import Path
app,run,state,prometheus,grafana,grafana_home=map(str,sys.argv[1:])
launch_agents=Path.home()/'Library'/'LaunchAgents'
launch_agents.mkdir(parents=True,exist_ok=True)
contracts={
 'exporter': {'@APP_DIR@':app,'@RUN_ROOT@':run},
 'prometheus': {'@APP_DIR@':app,'@RUN_ROOT@':run,'@STATE_DIR@':state,'@PROMETHEUS_BIN@':prometheus},
 'grafana': {'@APP_DIR@':app,'@RUN_ROOT@':run,'@STATE_DIR@':state,'@GRAFANA_BIN@':grafana,'@GRAFANA_HOME@':grafana_home},
}
for name,replacements in contracts.items():
    source=Path(app)/'ops'/'launchd'/f'com.polymarket.v7.{name}.plist.in'
    payload=source.read_text(encoding='utf-8')
    for marker,value in replacements.items():
        assert payload.count(marker)>=1, (name,marker)
        payload=payload.replace(marker,value)
    assert '@' not in payload, name
    destination=launch_agents/f'com.polymarket.v7.{name}.plist'
    temporary=destination.with_name(destination.name+f'.tmp.{os.getpid()}')
    temporary.write_text(payload,encoding='utf-8')
    os.chmod(temporary,0o644); os.replace(temporary,destination)
PY
    for label in exporter prometheus grafana; do
      destination="$HOME/Library/LaunchAgents/com.polymarket.v7.$label.plist"
      plutil -lint "$destination" >/dev/null
      launchctl bootstrap "$domain" "$destination"
    done
    sleep 1
    for label in exporter prometheus grafana; do
      launchctl print "$domain/com.polymarket.v7.$label" 2>/dev/null | grep -q 'state = running' || \
        fail "launchd monitoring service failed to start: $label"
    done
    return 0
  fi
  stop_owned_monitoring
  stop_stale_monitoring_listener exporter 9108 "$APP_DIR/monitoring/exporter_v7.py"
  stop_stale_monitoring_listener prometheus 9090 "prometheus"
  stop_stale_monitoring_listener grafana 3000 "grafana"
  nohup python3 "$APP_DIR/monitoring/exporter_v7.py" \
    --run-root "$run_root" --repository-root "$APP_DIR" --host 127.0.0.1 --port 9108 \
    >>"$run_root/monitoring-exporter.log" 2>&1 </dev/null &
  echo $! > "$STATE_DIR/exporter-v7.pid"
  if command -v prometheus >/dev/null 2>&1; then
    mkdir -p "$monitoring_root/prometheus"
    nohup prometheus --config.file="$STATE_DIR/prometheus-v7.yml" \
      --web.listen-address=127.0.0.1:9090 --storage.tsdb.path="$monitoring_root/prometheus" \
      >>"$run_root/prometheus-v7.log" 2>&1 </dev/null &
    echo $! > "$STATE_DIR/prometheus-v7.pid"
  fi
  if command -v grafana >/dev/null 2>&1; then
    local grafana_home="${POLYMARKET_GRAFANA_HOME:-/usr/share/grafana}"
    mkdir -p "$monitoring_root/grafana/data" "$monitoring_root/grafana/logs" "$monitoring_root/grafana/plugins"
    nohup env GF_SERVER_HTTP_ADDR=127.0.0.1 GF_SERVER_HTTP_PORT=3000 \
      GF_AUTH_ANONYMOUS_ENABLED=true GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer \
      GF_PATHS_PROVISIONING="$STATE_DIR/grafana/provisioning" GF_PATHS_DATA="$monitoring_root/grafana/data" \
      GF_PATHS_LOGS="$monitoring_root/grafana/logs" GF_PATHS_PLUGINS="$monitoring_root/grafana/plugins" \
      grafana server --homepath "$grafana_home" >>"$run_root/grafana-v7.log" 2>&1 </dev/null &
    echo $! > "$STATE_DIR/grafana-v7.pid"
  fi
}

runtime_health(){
  local run_root="$(production_run_root)" uid="$1"
  python3 - "$run_root" "$EXPECTED_SHA" <<'PY'
import csv,json,os,sys,time
from pathlib import Path
root=Path(sys.argv[1]); sha=sys.argv[2]; now=int(time.time())
required=[root/'control/runtime_status.json',root/'control/portfolio_state.json',root/'control/allocations/manifest.json',root/'control/research_sleeves_manifest.json',root/'osint/status.json',root/'osint/mapping_status.json',root/'shadow/sports_latency/component_status.json',root/'shadow/cross_platform/component_status.json',root/'market_open/status.json',root/'graph_rv/status.json',root/'external_fair/paper_router_status.json',root/'canonical_economics.json',root/'ledger/execution.jsonl',root/'trade_tape.csv']
assert all(p.exists() for p in required), [str(p) for p in required if not p.exists()]
runtime=json.loads((root/'control/runtime_status.json').read_text())
portfolio=json.loads((root/'control/portfolio_state.json').read_text())
graph=json.loads((root/'graph_rv/status.json').read_text())
router=json.loads((root/'external_fair/paper_router_status.json').read_text())
economics=json.loads((root/'canonical_economics.json').read_text())
assert runtime.get('version')==7 and runtime.get('model_sha')==sha
assert runtime.get('paper_only') is True and runtime.get('authenticated_execution') is False and runtime.get('real_order_submission') is False
assert all(str(runtime.get(k) or '') for k in ('config_hash','policy_hash','model_hash','run_id','ledger_id','server_id'))
pid=int(runtime.get('pid') or 0); assert pid>0; os.kill(pid,0)
assert now-int(runtime.get('timestamp') or 0)<=180
assert portfolio.get('paper_only') is True and portfolio.get('authenticated_execution') is False
assert portfolio.get('killed') is False and float(portfolio.get('drawdown',1))<.15
assert now-int(portfolio.get('timestamp') or 0)<=30
assert graph.get('paper_only') is True and graph.get('authenticated_execution') is False
assert now-int(graph.get('timestamp') or 0)<=180
assert router.get('code_sha')==sha and router.get('state')=='RUNNING'
assert router.get('paper_only') is True and router.get('authenticated_execution') is False and router.get('real_order_submission') is False
assert int(router.get('book_requests') or 0)>0
assert int((router.get('last_decision') or {}).get('books') or 0)==2
assert now-int(router.get('timestamp') or 0)<=30
assert economics.get('paper_only') is True and economics.get('authenticated_execution') is False
assert economics.get('expected_model_sha')==sha
with (root/'trade_tape.csv').open(newline='',encoding='utf-8') as handle: rows=list(csv.DictReader(handle))
assert rows and max(int(float(r.get('received_ms') or 0)) for r in rows)>0
PY
  [[ "$?" -eq 0 ]] || return 1
  curl -fsS http://127.0.0.1:9108/healthz >/dev/null || return 1
  local metrics
  metrics="$(curl -fsS http://127.0.0.1:9108/metrics)" || return 1
  grep -q '^polymarket_v7_runtime_info 1$' <<<"$metrics" || return 1
  grep -q '^polymarket_v7_execution_alive 1$' <<<"$metrics" || return 1
  grep -q '^polymarket_v7_supervisor_alive 1$' <<<"$metrics" || return 1
  grep -q '^polymarket_v7_single_writer_ok 1$' <<<"$metrics" || return 1
  grep -q '^polymarket_v7_exact_sha_ok 1$' <<<"$metrics" || return 1
  grep -q '^polymarket_v7_paper_only_contract_ok 1$' <<<"$metrics" || return 1
  grep -q '^polymarket_v7_authenticated_execution_disabled 1$' <<<"$metrics" || return 1
  grep -q '^polymarket_v7_ledger_valid 1$' <<<"$metrics" || return 1
  grep -q '^polymarket_v7_strategy_registry_enabled 15$' <<<"$metrics" || return 1
  grep -q '^polymarket_v7_research_sleeves_attached 3$' <<<"$metrics" || return 1
  grep -q '^polymarket_v7_research_supervisor_alive 1$' <<<"$metrics" || return 1
  grep -q '^polymarket_v7_research_manifest_fresh 1$' <<<"$metrics" || return 1
  grep -q '^polymarket_external_fair_present 1$' <<<"$metrics" || return 1
  awk '$1=="polymarket_external_fair_router_book_requests_total"{found=1; if ($2+0>0) ok=1} END{exit !(found&&ok)}' <<<"$metrics" || return 1
  curl -fsS http://127.0.0.1:9108/external-fair.json >/dev/null || return 1
  grep -q '^polymarket_v7_live_model_target_count 12$' <<<"$metrics" || return 1
  local operational blocked blocked_config blocked_external target_operational
  operational="$(awk '$1=="polymarket_v7_live_model_operational_count"{print int($2)}' <<<"$metrics")"
  blocked="$(awk '$1=="polymarket_v7_live_model_blocked_count"{print int($2)}' <<<"$metrics")"
  blocked_config="$(awk '$1=="polymarket_v7_live_model_blocked_config_count"{print int($2)}' <<<"$metrics")"
  blocked_external="$(awk '$1=="polymarket_v7_live_model_blocked_external_count"{print int($2)}' <<<"$metrics")"
  target_operational="$(awk '$1=="polymarket_v7_live_model_target_operational"{print int($2)}' <<<"$metrics")"
  test "$((operational + blocked))" -eq 12 || return 1
  test "$((blocked_config + blocked_external))" -eq "$blocked" || return 1
  if [[ "$operational" -eq 12 ]]; then
    test "$target_operational" -eq 1 || return 1
  else
    test "$target_operational" -eq 0 || return 1
  fi
  grep -q '^polymarket_v7_live_model_scope_wired 1$' <<<"$metrics" || return 1
  curl -fsS http://127.0.0.1:9090/-/ready >/dev/null || return 1
  curl -fsS http://127.0.0.1:3000/api/health >/dev/null || return 1
  curl -fsS "http://127.0.0.1:3000/api/dashboards/uid/$uid" >/dev/null || return 1
}

http_diagnostic(){
  local label="$1" url="$2" tmp code
  tmp="$(mktemp)"
  code="$(curl -sS -o "$tmp" -w '%{http_code}' "$url" 2>/dev/null || true)"
  printf '[v7-health] %s http=%s url=%s\n' "$label" "${code:-curl_error}" "$url" >&2
  if [[ -s "$tmp" ]]; then
    head -c 800 "$tmp" >&2 || true
    printf '\n' >&2
  fi
  rm -f "$tmp"
}

runtime_health_diagnostics(){
  local uid="$1" run_root="$(production_run_root)"
  http_diagnostic exporter_health http://127.0.0.1:9108/healthz
  http_diagnostic exporter_metrics http://127.0.0.1:9108/metrics
  http_diagnostic prometheus_ready http://127.0.0.1:9090/-/ready
  http_diagnostic grafana_health http://127.0.0.1:3000/api/health
  http_diagnostic grafana_search http://127.0.0.1:3000/api/search
  http_diagnostic grafana_dashboard "http://127.0.0.1:3000/api/dashboards/uid/$uid"
  printf '%s\n' '--- grafana-v7.log ---' >&2
  tail -n 160 "$run_root/grafana-v7.log" >&2 2>/dev/null || true
  printf '%s\n' '--- prometheus-v7.log ---' >&2
  tail -n 80 "$run_root/prometheus-v7.log" >&2 2>/dev/null || true
  printf '%s\n' '--- monitoring-exporter.log ---' >&2
  tail -n 80 "$run_root/monitoring-exporter.log" >&2 2>/dev/null || true
  printf '%s\n' '--- external-fair-paper-router ---' >&2
  head -c 4000 "$run_root/external_fair/paper_router_status.json" >&2 2>/dev/null || true
  printf '\n' >&2
}

cd "$APP_DIR"
write_status validating "fetching canonical refs"
git fetch --no-tags origin "$DEPLOY_REF"
MAIN_SHA="$(git rev-parse "origin/$DEPLOY_REF")"
[[ "$MAIN_SHA" == "$EXPECTED_SHA" ]] || fail "origin/main $MAIN_SHA != exact approved SHA $EXPECTED_SHA"
prevalidate_candidate
OLD_SHA="$(resolve_incumbent_sha)"
[[ "$OLD_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "cannot resolve exact deployed incumbent SHA"
git merge-base --is-ancestor "$OLD_SHA" "$EXPECTED_SHA" >/dev/null 2>&1 || \
  fail "deployed incumbent $OLD_SHA is not an ancestor of target $EXPECTED_SHA"
[[ -z "$(git status --porcelain --untracked-files=no)" ]] || fail "tracked server checkout is dirty"
request_cutover_drain "$OLD_SHA"
wait_for_cutover_drain "$OLD_SHA"
MAKER_STATUS_REFRESHER="${POLYMARKET_MAKER_STATUS_REFRESHER:-$candidate/scripts/v7_market_maker_status.py}"
[[ -f "$MAKER_STATUS_REFRESHER" ]] || fail "maker status refresher missing: $MAKER_STATUS_REFRESHER"
stop_production_runtime
if [[ "$OLD_SHA" != "$EXPECTED_SHA" ]]; then
  # Refresh from immutable state only after BLUE is stopped. This removes any
  # race with late fills and keeps the finalizer's 15-second mark freshness
  # contract independent of process-shutdown latency.
  env https_proxy=http://127.0.0.1:19109 http_proxy=http://127.0.0.1:19109 \
    HTTPS_PROXY=http://127.0.0.1:19109 HTTP_PROXY=http://127.0.0.1:19109 \
    no_proxy=127.0.0.1,localhost NO_PROXY=127.0.0.1,localhost \
    python3 "$MAKER_STATUS_REFRESHER" \
      --state "$(production_run_root)/micro_maker/state.json" \
      --config "$(production_run_root)/control/allocations/micro_maker.json" \
      --selection "$(production_run_root)/micro_maker/reward_selection.json" \
      --output "$(production_run_root)/control/maker_cutover_mark.json" \
      --cutover-zero-recovery \
      >/dev/null
fi
MAKER_CUTOVER_FINALIZER="${POLYMARKET_MAKER_CUTOVER_FINALIZER:-$candidate/scripts/v7_finalize_maker_cutover.py}"
[[ -f "$MAKER_CUTOVER_FINALIZER" ]] || fail "maker cutover finalizer missing: $MAKER_CUTOVER_FINALIZER"
if [[ "$OLD_SHA" != "$EXPECTED_SHA" ]]; then
  python3 "$MAKER_CUTOVER_FINALIZER" \
    --run-root "$(production_run_root)" \
    --model-sha "$OLD_SHA" \
    --nonce "$LOCK_NONCE" \
    --mark "$(production_run_root)/control/maker_cutover_mark.json" | tee -a deploy-evidence.txt
fi
stop_owned_monitoring
CUTOVER_ARCHIVER="${POLYMARKET_CUTOVER_ARCHIVER:-$APP_DIR/scripts/v7_prepare_cutover_run_root.py}"
[[ -f "$CUTOVER_ARCHIVER" ]] || fail "cutover archiver missing: $CUTOVER_ARCHIVER"
python3 "$CUTOVER_ARCHIVER" \
  --run-root "$(production_run_root)" \
  --archive-root "$APP_DIR/runs/paper_v7_archives" \
  --repository-root "$APP_DIR" \
  --target-sha "$EXPECTED_SHA" | tee -a deploy-evidence.txt
if [[ "$OLD_SHA" != "$EXPECTED_SHA" ]]; then
  git checkout --detach "$EXPECTED_SHA"
  git reset --hard "$EXPECTED_SHA"
fi
python3 scripts/v7_cutover_contract.py --repository-root "$APP_DIR" --expected-head "$EXPECTED_SHA" >/dev/null
DASHBOARD_UID="$(monitoring_contract "$APP_DIR")"
build_current_checkout
start_production_runtime
[[ "$(git rev-parse HEAD)" == "$EXPECTED_SHA" ]] || fail "server checkout drifted immediately after runtime start"
record_deployed_sha
write_status running "exact V7 SHA started; monitoring health pending"
start_monitoring
healthy=0
for _ in $(seq 1 "$HEALTH_ATTEMPTS"); do
  if runtime_health "$DASHBOARD_UID" >/dev/null 2>&1; then healthy=1; break; fi
  sleep 1
done
if [[ "$healthy" != 1 ]]; then
  write_status failed "post-deploy canonical V7 health did not converge"
  runtime_health "$DASHBOARD_UID" || true
  runtime_health_diagnostics "$DASHBOARD_UID" || true
  fail "canonical V7 runtime/monitoring health failed"
fi
[[ "$(git rev-parse HEAD)" == "$EXPECTED_SHA" ]] || fail "server checkout drifted after deployment"
record_deployed_sha
record_incumbent_identity
write_status healthy "canonical V7 PAPER runtime and monitoring healthy"
log "V7 deployed exact SHA $EXPECTED_SHA"
