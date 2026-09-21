#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
source scripts/v7_process_runtime.sh

COLLECTION_ROOT="${PM_V7_COLLECTION_ROOT:-/mnt/polymarket-data/polymarket_v7_collection}"
COLLECTION_SHA="${PM_V7_COLLECTION_SHA:-$(cat deploy/london/runtime_sha 2>/dev/null || git rev-parse HEAD)}"
PUBLIC_PROXY_PORT="${PM_V7_COLLECTION_PROXY_PORT:-19110}"
RECORDER="${PM_V7_COLLECTION_TRADE_RECORDER:-$ROOT/build/polymarket_v7_trade_recorder}"
FILLABILITY_OBSERVER="${PM_V7_COLLECTION_BOOK_OBSERVER:-$ROOT/build/polymarket_v7_maker_fillability_observer}"
EXTERNAL_VENUE_RUNTIME="${PM_V7_COLLECTION_EXTERNAL_RUNTIME:-$ROOT/build/polymarket_v7_external_venue_runtime}"
UNIVERSE_CONFIG="$ROOT/config/v7_crypto_universe.json"
MARKET_REGISTRY="$ROOT/config/v7_crypto_settlement_markets.json"
RETENTION_CONFIG="$ROOT/config/v7_london_buffer_retention.json"
EXECUTION_ALPHA_CONFIG="$ROOT/config/v7_crypto_execution_alpha.json"
CONTROL="$COLLECTION_ROOT/control"
LOCK="$CONTROL/collection.lock"
DISK_PRESSURE_MARKER="$CONTROL/DISK_PRESSURE"

[[ "$COLLECTION_ROOT" == /* ]] || { echo "collection root must be absolute" >&2; exit 78; }
[[ "$COLLECTION_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "exact collector SHA required" >&2; exit 78; }
[[ "$PUBLIC_PROXY_PORT" =~ ^[1-9][0-9]*$ ]] || { echo "invalid collection proxy port" >&2; exit 78; }
[[ -x "$RECORDER" ]] || { echo "missing collection trade recorder" >&2; exit 78; }
[[ -x "$FILLABILITY_OBSERVER" ]] || { echo "missing collection PM book observer" >&2; exit 78; }
[[ -x "$EXTERNAL_VENUE_RUNTIME" ]] || { echo "missing collection external venue runtime" >&2; exit 78; }

mkdir -p "$CONTROL" "$COLLECTION_ROOT/universe" "$COLLECTION_ROOT/external_fair"   "$COLLECTION_ROOT/research/repricing_book" "$COLLECTION_ROOT/market_data"

if ! mkdir "$LOCK" 2>/dev/null; then
  old_pid="$(cat "$LOCK/pid" 2>/dev/null || true)"
  if [[ "$old_pid" =~ ^[1-9][0-9]*$ ]] && kill -0 "$old_pid" 2>/dev/null; then
    echo "collection plane already active pid=$old_pid" >&2
    exit 73
  fi
  rm -rf "$LOCK"
  mkdir "$LOCK"
fi
echo $$ > "$LOCK/pid"

pids=()
fatal_pids=()
names=()

register_named_child() {
  local name="$1" pid="$2"
  names+=("$name")
  v7_register_child "$pid"
}

write_status() {
  local state="$1"
  python3 - "$CONTROL/runtime_status.json" "$COLLECTION_SHA" "$$" "$state"     "${names[*]}" "${pids[*]}" "$COLLECTION_ROOT" <<'PY'
import json,os,sys,time
from pathlib import Path
path=Path(sys.argv[1]); sha=sys.argv[2]; supervisor=int(sys.argv[3]); state=sys.argv[4]
names=sys.argv[5].split(); pids=[int(v) for v in sys.argv[6].split()] if sys.argv[6].strip() else []
root=Path(sys.argv[7])
children=[]
for index,name in enumerate(names):
    pid=pids[index] if index < len(pids) else 0
    alive=False
    if pid>0:
        try: os.kill(pid,0); alive=True
        except OSError: pass
    children.append({"name":name,"pid":pid,"alive":alive})
now_ns=time.time_ns()
value={
  "schema":"polymarket_v7_collection_plane_status_v1",
  "version":1,
  "timestamp":int(time.time()),
  "timestamp_ns":now_ns,
  "state":state,
  "collector_sha":sha,
  "model_sha":sha,
  "model_sha_semantics":"COLLECTOR_CODE_PROVENANCE_ONLY_NOT_TRADING_MODEL",
  "model_independent":True,
  "live_model_required":False,
  "paper_only":True,
  "authenticated_execution":False,
  "real_order_submission":False,
  "real_capital_at_risk":False,
  "execution_authority":"ZERO_AUTHORITY_DATA_COLLECTION",
  "supervisor_pid":supervisor,
  "collection_root":str(root),
  "children":children,
}
tmp=path.with_name(path.name+f".tmp.{os.getpid()}")
tmp.write_text(json.dumps(value,sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8")
os.replace(tmp,path)
PY
}

cleanup_started=0
cleanup() {
  local rc=$?
  if [[ "$cleanup_started" == 1 ]]; then return "$rc"; fi
  cleanup_started=1
  set +e
  write_status "STOPPING"
  for pid in "${pids[@]:-}"; do kill -TERM "$pid" 2>/dev/null || true; done
  for _ in $(seq 1 50); do
    alive=0
    for pid in "${pids[@]:-}"; do kill -0 "$pid" 2>/dev/null && { alive=1; break; }; done
    [[ "$alive" == 0 ]] && break
    sleep 0.1
  done
  for pid in "${pids[@]:-}"; do
    kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  done
  if [[ "$(cat "$LOCK/pid" 2>/dev/null || true)" == "$$" ]]; then rm -rf "$LOCK"; fi
  write_status "STOPPED"
  return "$rc"
}
trap cleanup EXIT INT TERM

# The collection plane deliberately does not consume the trading runtime's
# HOT/COLLECTOR/CONTROL CPU partition. Data capture must survive model/runtime
# cpuset changes and hosts with fewer visible CPUs. Optional collector affinity
# may be supplied explicitly; otherwise collectors run unpinned at low priority.
PM_V7_COLLECTOR_CPUSET="${PM_V7_COLLECTION_CPUSET:-${PM_V7_COLLECTOR_CPUSET:-}}"
PM_V7_COLLECTOR_NICE="${PM_V7_COLLECTION_NICE:-10}"
[[ "$PM_V7_COLLECTOR_NICE" =~ ^[0-9]+$ ]] && (( PM_V7_COLLECTOR_NICE <= 19 )) || {
  echo "collection nice must be an integer in [0,19]" >&2; exit 74;
}
if [[ -n "$PM_V7_COLLECTOR_CPUSET" ]]; then
  [[ "$PM_V7_COLLECTOR_CPUSET" =~ ^[0-9]+([,-][0-9]+)*$ ]] || {
    echo "invalid optional collection cpuset" >&2; exit 74;
  }
fi
export PM_V7_COLLECTOR_CPUSET PM_V7_COLLECTOR_NICE
python3 - "$CONTROL/collection_resource_plan.json" "$PM_V7_COLLECTOR_CPUSET" "$PM_V7_COLLECTOR_NICE" <<'PY'
import json,os,sys
from pathlib import Path
path=Path(sys.argv[1])
value={
  "schema":"polymarket_v7_collection_resource_plan_v1",
  "paper_only":True,
  "model_independent":True,
  "trading_runtime_resource_plan_required":False,
  "collector_cpuset":sys.argv[2] or None,
  "collector_nice":int(sys.argv[3]),
  "visible_cpu_count":len(os.sched_getaffinity(0)) if hasattr(os,"sched_getaffinity") else (os.cpu_count() or 1),
}
temporary=path.with_name(path.name+f".tmp.{os.getpid()}")
temporary.write_text(json.dumps(value,sort_keys=True,indent=2)+"\n",encoding="utf-8")
os.replace(temporary,path)
PY

DISK_PRESSURE_MIN_FREE_BYTES="$(python3 - "$RETENTION_CONFIG" <<'PY'
import json,sys
v=json.load(open(sys.argv[1],encoding='utf-8'))
x=v['disk_pressure_min_free_bytes']
assert type(x) is int and x>0
print(x)
PY
)"

EXTERNAL_CANCEL_RULE_SHA="$(python3 - "$EXECUTION_ALPHA_CONFIG" <<'PY'
import hashlib,json,sys
value=json.load(open(sys.argv[1],encoding='utf-8'))
rule=value['execution_alpha']['cancel']['research_rule']
assert value.get('paper_only') is True
assert value.get('real_order_submission') is False
assert value['execution_alpha']['cancel'].get('research_only') is True
raw=json.dumps(rule,sort_keys=True,separators=(',',':')).encode()
print(hashlib.sha256(raw).hexdigest())
PY
)"

write_status "STARTING"

v7_exec_class COLLECTOR python3 scripts/v7_public_https_proxy.py   --host 127.0.0.1 --port "$PUBLIC_PROXY_PORT"   >> "$COLLECTION_ROOT/public_https_proxy.log" 2>&1 &
register_named_child proxy "$!"

proxy_ready=0
for _ in $(seq 1 100); do
  if python3 - "$PUBLIC_PROXY_PORT" <<'PY' >/dev/null 2>&1
import socket,sys
with socket.create_connection(("127.0.0.1",int(sys.argv[1])),timeout=.2): pass
PY
  then proxy_ready=1; break; fi
  sleep 0.1
done
[[ "$proxy_ready" == 1 ]] || { echo "collection public proxy unavailable" >&2; exit 77; }

PUBLIC_PROXY="http://127.0.0.1:$PUBLIC_PROXY_PORT"
export PM_V7_HTTPS_PROXY="$PUBLIC_PROXY" HTTPS_PROXY="$PUBLIC_PROXY" https_proxy="$PUBLIC_PROXY"
export HTTP_PROXY="$PUBLIC_PROXY" http_proxy="$PUBLIC_PROXY"
export NO_PROXY="127.0.0.1,localhost"
export no_proxy="$NO_PROXY"
if [[ -z "${PM_V7_WS_RESOLVE_IPS:-}" ]]; then
  PM_V7_WS_RESOLVE_IPS="$(python3 scripts/v7_public_https_proxy.py --resolve ws-subscriptions-clob.polymarket.com)"
fi
[[ -n "$PM_V7_WS_RESOLVE_IPS" ]] || { echo "collection WS resolution empty" >&2; exit 77; }
export PM_V7_WS_RESOLVE_IPS

v7_exec_class COLLECTOR python3 scripts/v7_crypto_universe.py   --config "$UNIVERSE_CONFIG" --output-dir "$COLLECTION_ROOT/universe"   --model-sha "$COLLECTION_SHA" --loop   >> "$COLLECTION_ROOT/universe/collector.log" 2>&1 &
register_named_child universe "$!"

v7_exec_class COLLECTOR python3 scripts/v7_multi_asset_external_collector.py   --repository-root "$ROOT" --run-root "$COLLECTION_ROOT" --model-sha "$COLLECTION_SHA"   --engine "$EXTERNAL_VENUE_RUNTIME" --market-registry "$MARKET_REGISTRY"   --disk-pressure-marker "$DISK_PRESSURE_MARKER"   --disk-pressure-min-free-bytes "$DISK_PRESSURE_MIN_FREE_BYTES"   --external-cancel-signal "$COLLECTION_ROOT/external_fair/external_cancel_signal.json"   --external-cancel-rule-sha256 "$EXTERNAL_CANCEL_RULE_SHA"   >> "$COLLECTION_ROOT/external_fair/external_assets_supervisor.log" 2>&1 &
register_named_child external "$!"

universe_ready=0
for _ in $(seq 1 600); do
  if python3 - "$COLLECTION_ROOT/universe/status.json" "$COLLECTION_SHA" <<'PY' >/dev/null 2>&1
import json,sys,time
v=json.load(open(sys.argv[1],encoding='utf-8'))
ok=(v.get('schema')=='polymarket_v7_crypto_universe_status_v1'
    and v.get('model_sha')==sys.argv[2] and v.get('state')=='OPERATIONAL'
    and v.get('discovery_exhaustive') is True
    and int(v.get('book_selection_contexts') or 0)==30
    and int(v.get('book_selection_tokens') or 0)==60
    and v.get('paper_only') is True
    and v.get('authenticated_execution') is False
    and v.get('real_order_submission') is False)
raise SystemExit(0 if ok else 1)
PY
  then universe_ready=1; break; fi
  sleep 0.5
done
[[ "$universe_ready" == 1 ]] || { echo "collection universe not ready" >&2; exit 77; }

v7_exec_class COLLECTOR python3 scripts/v7_rtds_external_fair_monitor.py   --output-dir "$COLLECTION_ROOT/external_fair" --code-sha "$COLLECTION_SHA"   --universe "$COLLECTION_ROOT/universe/current.json"   --approvals "$ROOT/config/v7_external_fair_rule_approvals.json"   --external-venues "$COLLECTION_ROOT/external_fair/external_venues.json"   --external-fair-config "$ROOT/config/v7_external_fair.json"   >> "$COLLECTION_ROOT/external_fair/rtds_monitor.log" 2>&1 &
register_named_child rtds "$!"

v7_exec_class COLLECTOR "$FILLABILITY_OBSERVER"   --config "$ROOT/config/paper_v7.json"   --run-root "$COLLECTION_ROOT" --model-sha "$COLLECTION_SHA"   --selection "$COLLECTION_ROOT/universe/book_selection.json" --selection-only   --output-dir "$COLLECTION_ROOT/research/repricing_book"   --disk-pressure-min-free-bytes "$DISK_PRESSURE_MIN_FREE_BYTES"   >> "$COLLECTION_ROOT/research/repricing_book_observer.log" 2>&1 &
register_named_child pm_book "$!"

v7_exec_class COLLECTOR "$RECORDER"   --run-dir "$COLLECTION_ROOT"   --universe "$COLLECTION_ROOT/universe/current.json" --model-sha "$COLLECTION_SHA"   --data-url "https://data-api.polymarket.com" --batch 40   --lookback-seconds 600 --interval 5 --loop   >> "$COLLECTION_ROOT/trade_recorder.log" 2>&1 &
register_named_child public_trades "$!"

v7_assert_registered_child_count 6
write_status "COLLECTING"

while true; do
  for pid in "${fatal_pids[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "collection child exited pid=$pid" >&2
      exit 75
    fi
  done
  write_status "COLLECTING"
  sleep 1
done
