#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# The live runtime is selected explicitly. A numerically newer paper_vN implementation
# becomes live only through the validated integration pipeline; promotion may be
# performed automatically by the scheduler once objective gates pass.
manifest="${POLYMARKET_CHAMPION_MANIFEST:-$ROOT/config/live_champion.json}"
[[ -f "$manifest" ]] || {
  echo "fatal: live champion manifest not found: $manifest" >&2
  exit 1
}

champion="$({
  python3 - "$manifest" <<'PY'
import json
import sys
from pathlib import PurePosixPath

path = sys.argv[1]
with open(path, encoding="utf-8") as handle:
    data = json.load(handle)

required = {
    "schema_version",
    "version",
    "loop",
    "config",
    "run_root",
    "deployment_ref",
    "promotion_policy",
}
missing = sorted(required.difference(data))
if missing:
    raise SystemExit(f"champion manifest missing keys: {', '.join(missing)}")

version = data["version"]
if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
    raise SystemExit("champion version must be a positive integer")
if data["schema_version"] != 1:
    raise SystemExit("unsupported champion manifest schema")

loop = str(data["loop"])
config = str(data["config"])
run_root = str(data["run_root"])
deployment_ref = str(data["deployment_ref"])
promotion_policy = str(data["promotion_policy"])

for key, value in (("loop", loop), ("config", config), ("run_root", run_root)):
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or ".." in parsed.parts:
        raise SystemExit(f"champion {key} must be a repository-relative path")

expected_loop = f"scripts/paper_v{version}_loop.sh"
expected_config = f"config/paper_v{version}.json"
expected_run_root = f"runs/paper_v{version}_live"
if loop != expected_loop:
    raise SystemExit(f"champion loop must be {expected_loop}")
if config != expected_config:
    raise SystemExit(f"champion config must be {expected_config}")
if run_root != expected_run_root:
    raise SystemExit(f"champion run_root must be {expected_run_root}")
if deployment_ref != "paper-validated":
    raise SystemExit("live deployment_ref must remain paper-validated")
if promotion_policy != "automatic validated integration":
    raise SystemExit("unsupported live promotion policy")

print("\t".join((str(version), loop, config, run_root, deployment_ref)))
PY
} 2>&1)" || {
  echo "fatal: invalid live champion manifest: $champion" >&2
  exit 1
}

IFS=$'\t' read -r version loop_rel config_rel run_root_rel deployment_ref <<<"$champion"
loop="$ROOT/$loop_rel"
config="${POLYMARKET_CONFIG:-$ROOT/$config_rel}"
run_root="${POLYMARKET_RUN_ROOT:-$ROOT/$run_root_rel}"

[[ -f "$loop" ]] || { echo "fatal: champion loop missing: $loop" >&2; exit 1; }
[[ -f "$config" ]] || { echo "fatal: champion config missing: $config" >&2; exit 1; }

mkdir -p "$run_root"
printf 'paper_champion version=%s loop=%s config=%s run_root=%s deploy_ref=%s manifest=%s\n' \
  "$version" "$loop_rel" "$config" "$run_root" "$deployment_ref" "$manifest"

if [[ "${1:-}" == "--print-champion" ]]; then
  exit 0
fi

# Only one manifest-selected paper runtime may own a run root at a time. This
# prevents deploy/restart overlap from creating concurrent state writers. The
# lock is PID-bound and stale locks are recovered without touching other roots.
runtime_lock="$run_root/.paper_latest_singleton"
lock_owner_file="$runtime_lock/pid"
lock_owned=0
acquire_runtime_lock() {
  if mkdir "$runtime_lock" 2>/dev/null; then
    printf '%s\n' "$$" > "$lock_owner_file"
    lock_owned=1
    return 0
  fi
  local owner=""
  owner="$(cat "$lock_owner_file" 2>/dev/null || true)"
  if [[ "$owner" =~ ^[0-9]+$ ]] && kill -0 "$owner" 2>/dev/null; then
    echo "fatal: manifest-selected paper runtime already active pid=$owner run_root=$run_root" >&2
    return 1
  fi
  rm -rf "$runtime_lock"
  mkdir "$runtime_lock"
  printf '%s\n' "$$" > "$lock_owner_file"
  lock_owned=1
}
release_runtime_lock() {
  if [[ "$lock_owned" == "1" ]]; then
    local owner=""
    owner="$(cat "$lock_owner_file" 2>/dev/null || true)"
    if [[ "$owner" == "$$" ]]; then rm -rf "$runtime_lock"; fi
    lock_owned=0
  fi
}
acquire_runtime_lock

# V6 public-market discovery is made resilient at the outer operational layer,
# so the economic V6 loop remains byte-for-byte unchanged. The local proxy
# prefers Gamma keyset pagination, falls back to public CLOB markets+books, and
# only then serves a bounded metadata cache. No authenticated endpoint is used.
proxy_enabled=0
proxy_pid=0
proxy_restarts=0
proxy_port="${V6_MARKET_PROXY_PORT:-$((19000 + ($$ % 1000)))}"
proxy_script="$ROOT/scripts/v6_market_proxy.py"
base_config="$config"
if [[ "$version" == "6" && "${POLYMARKET_V6_MARKET_PROXY_ENABLED:-1}" == "1" ]]; then
  [[ -f "$proxy_script" ]] || {
    release_runtime_lock
    echo "fatal: V6 market proxy missing: $proxy_script" >&2
    exit 1
  }
  read -r upstream_gamma upstream_clob < <(python3 - "$base_config" <<'PY'
import json,sys
with open(sys.argv[1], encoding='utf-8') as handle:
    cfg=json.load(handle)
print(str(cfg.get('gamma_url','https://gamma-api.polymarket.com')), str(cfg.get('clob_url','https://clob.polymarket.com')))
PY
  )
  runtime_config="$run_root/runtime_config.json"
  python3 - "$base_config" "$runtime_config" "$proxy_port" <<'PY'
import json,os,sys
from pathlib import Path
src=Path(sys.argv[1]); dst=Path(sys.argv[2]); port=int(sys.argv[3])
cfg=json.loads(src.read_text(encoding='utf-8'))
cfg['gamma_url']=f'http://127.0.0.1:{port}'
tmp=dst.with_name(dst.name+f'.tmp.{os.getpid()}')
tmp.write_text(json.dumps(cfg,indent=2,sort_keys=True)+'\n',encoding='utf-8')
os.replace(tmp,dst)
PY
  config="$runtime_config"
  proxy_enabled=1
fi

fast_enabled="${POLYMARKET_FAST_ARB_ENABLED:-1}"
# Shadow instrumentation must never block the incumbent champion by default.
# Operators may set REQUIRED=1 on a validated deployment to fail closed on a
# packaging/configuration regression without turning research into execution.
fast_required="${POLYMARKET_FAST_ARB_REQUIRED:-0}"
fast_binary="$ROOT/build/polymarket_fast_arb_shadow"
fast_policy="${POLYMARKET_FAST_ARB_POLICY:-$ROOT/config/fast_arb_policy.json}"
fast_relations="${POLYMARKET_FAST_ARB_RELATIONS:-$ROOT/config/fast_arb_relations.csv}"
fast_run_root="${POLYMARKET_FAST_ARB_RUN_ROOT:-$run_root/fast}"
fast_markets="${POLYMARKET_FAST_ARB_MARKETS:-1000}"
fast_min_liquidity="${POLYMARKET_FAST_ARB_MIN_LIQUIDITY:-50}"
fast_shard_size="${POLYMARKET_FAST_ARB_SHARD_SIZE:-200}"
fast_recycle_seconds="${POLYMARKET_FAST_ARB_RECYCLE_SECONDS:-900}"
fast_snapshot_seconds="${POLYMARKET_FAST_ARB_SNAPSHOT_SECONDS:-30}"

shadow_dependency_failure() {
  local message="$1"
  if [[ "$fast_required" == "1" ]]; then
    echo "fatal: $message" >&2
    exit 1
  fi
  echo "warning: $message; champion continues without fast shadow" >&2
  fast_enabled=0
}

if [[ "$fast_enabled" == "1" && ! -x "$fast_binary" ]]; then
  shadow_dependency_failure "fast-arbitrage shadow binary missing: $fast_binary"
fi
if [[ "$fast_enabled" == "1" && ! -f "$fast_policy" ]]; then
  shadow_dependency_failure "fast-arbitrage policy missing: $fast_policy"
fi
if [[ "$fast_enabled" == "1" && ! -f "$fast_relations" ]]; then
  shadow_dependency_failure "fast-arbitrage relation manifest missing: $fast_relations"
fi

mkdir -p "$fast_run_root"
champion_pid=0
fast_pid=0
fast_restarts=0

start_proxy() {
  if [[ "$proxy_enabled" != "1" ]]; then return 0; fi
  PYTHONUNBUFFERED=1 python3 "$proxy_script" \
    --host 127.0.0.1 \
    --port "$proxy_port" \
    --gamma "$upstream_gamma" \
    --clob "$upstream_clob" \
    --cache "$run_root/market_proxy_cache.json" \
    --status "$run_root/market_proxy_status.json" \
    >> "$run_root/market_proxy.log" 2>&1 &
  proxy_pid=$!
}

wait_proxy_ready() {
  if [[ "$proxy_enabled" != "1" ]]; then return 0; fi
  local ready=0
  local _
  for _ in {1..50}; do
    if curl -fsS "http://127.0.0.1:${proxy_port}/healthz" >/dev/null 2>&1; then
      ready=1
      break
    fi
    kill -0 "$proxy_pid" 2>/dev/null || break
    sleep 0.1
  done
  [[ "$ready" == "1" ]]
}

start_champion() {
  /usr/bin/env bash "$loop" "$config" "$run_root" &
  champion_pid=$!
}

start_fast() {
  "$fast_binary" \
    --config "$config" \
    --policy "$fast_policy" \
    --relations "$fast_relations" \
    --run-dir "$fast_run_root" \
    --markets "$fast_markets" \
    --min-liquidity "$fast_min_liquidity" \
    --shard-size "$fast_shard_size" \
    --snapshot-refresh-seconds "$fast_snapshot_seconds" \
    --duration-seconds 0 \
    --recycle-seconds "$fast_recycle_seconds" \
    >> "$fast_run_root/fast_runtime.log" 2>&1 &
  fast_pid=$!
}

write_plane_status() {
  local champion_alive=0
  local fast_alive=0
  if (( champion_pid > 0 )) && kill -0 "$champion_pid" 2>/dev/null; then champion_alive=1; fi
  if [[ "$fast_enabled" == "1" ]] && (( fast_pid > 0 )) && kill -0 "$fast_pid" 2>/dev/null; then fast_alive=1; fi
  local path="$run_root/runtime_planes.csv"
  local tmp="$path.tmp"
  printf 'timestamp,champion_version,champion_alive,fast_enabled,fast_alive,fast_restarts,champion_pid,fast_pid\n' > "$tmp"
  printf '%s,%s,%s,%s,%s,%s,%s,%s\n' "$(date +%s)" "$version" "$champion_alive" \
    "$fast_enabled" "$fast_alive" "$fast_restarts" "$champion_pid" "$fast_pid" >> "$tmp"
  mv "$tmp" "$path"
}

cleanup() {
  if (( fast_pid > 0 )); then kill "$fast_pid" 2>/dev/null || true; fi
  if (( champion_pid > 0 )); then kill "$champion_pid" 2>/dev/null || true; fi
  if (( proxy_pid > 0 )); then kill "$proxy_pid" 2>/dev/null || true; fi
  if (( fast_pid > 0 )); then wait "$fast_pid" 2>/dev/null || true; fi
  if (( champion_pid > 0 )); then wait "$champion_pid" 2>/dev/null || true; fi
  if (( proxy_pid > 0 )); then wait "$proxy_pid" 2>/dev/null || true; fi
  release_runtime_lock
}

shutdown() {
  trap - EXIT INT TERM
  cleanup
  exit 0
}

trap cleanup EXIT
trap shutdown INT TERM

if [[ "$proxy_enabled" == "1" ]]; then
  start_proxy
  if ! wait_proxy_ready; then
    echo "fatal: V6 market proxy failed to start" >&2
    exit 1
  fi
fi
start_champion
if [[ "$fast_enabled" == "1" ]]; then start_fast; fi
write_plane_status

while true; do
  if [[ "$proxy_enabled" == "1" ]] && ! kill -0 "$proxy_pid" 2>/dev/null; then
    wait "$proxy_pid" 2>/dev/null || true
    proxy_restarts=$((proxy_restarts + 1))
    printf '%s,market_proxy,restart,%s\n' "$(date +%s)" "$proxy_restarts" \
      >> "$run_root/runtime_plane_events.csv"
    sleep 1
    start_proxy
    if ! wait_proxy_ready; then
      echo "fatal: V6 market proxy restart failed" >&2
      exit 1
    fi
  fi

  if ! kill -0 "$champion_pid" 2>/dev/null; then
    set +e
    wait "$champion_pid"
    champion_rc=$?
    set -e
    echo "fatal: champion runtime exited rc=$champion_rc" >&2
    exit "$champion_rc"
  fi

  if [[ "$fast_enabled" == "1" ]] && ! kill -0 "$fast_pid" 2>/dev/null; then
    wait "$fast_pid" 2>/dev/null || true
    fast_restarts=$((fast_restarts + 1))
    printf '%s,fast_arb,restart,%s\n' "$(date +%s)" "$fast_restarts" \
      >> "$run_root/runtime_plane_events.csv"
    sleep 1
    start_fast
  fi

  write_plane_status
  sleep 5
done
