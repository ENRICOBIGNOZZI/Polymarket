#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# The live runtime is selected explicitly. Merely committing a numerically newer
# paper_vN implementation must never promote unapproved research into the live
# process.
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
if promotion_policy != "approved integration PR only":
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

fast_enabled="${POLYMARKET_FAST_ARB_ENABLED:-1}"
fast_required="${POLYMARKET_FAST_ARB_REQUIRED:-1}"
fast_binary="$ROOT/build/polymarket_fast_arb_shadow"
fast_policy="${POLYMARKET_FAST_ARB_POLICY:-$ROOT/config/fast_arb_policy.json}"
fast_relations="${POLYMARKET_FAST_ARB_RELATIONS:-$ROOT/config/fast_arb_relations.csv}"
fast_run_root="${POLYMARKET_FAST_ARB_RUN_ROOT:-$run_root/fast}"
fast_markets="${POLYMARKET_FAST_ARB_MARKETS:-1000}"
fast_min_liquidity="${POLYMARKET_FAST_ARB_MIN_LIQUIDITY:-50}"
fast_shard_size="${POLYMARKET_FAST_ARB_SHARD_SIZE:-200}"
fast_recycle_seconds="${POLYMARKET_FAST_ARB_RECYCLE_SECONDS:-900}"
fast_snapshot_seconds="${POLYMARKET_FAST_ARB_SNAPSHOT_SECONDS:-30}"

if [[ "$fast_enabled" == "1" ]]; then
  if [[ ! -x "$fast_binary" ]]; then
    if [[ "$fast_required" == "1" ]]; then
      echo "fatal: required fast-arbitrage shadow binary missing: $fast_binary" >&2
      exit 1
    fi
    echo "warning: fast-arbitrage shadow disabled because binary is missing" >&2
    fast_enabled=0
  fi
  [[ -f "$fast_policy" ]] || { echo "fatal: fast policy missing: $fast_policy" >&2; exit 1; }
  [[ -f "$fast_relations" ]] || { echo "fatal: relation manifest missing: $fast_relations" >&2; exit 1; }
fi

mkdir -p "$fast_run_root"
champion_pid=0
fast_pid=0
fast_restarts=0

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
  if (( fast_pid > 0 )); then wait "$fast_pid" 2>/dev/null || true; fi
  if (( champion_pid > 0 )); then wait "$champion_pid" 2>/dev/null || true; fi
}

shutdown() {
  trap - EXIT INT TERM
  cleanup
  exit 0
}

trap cleanup EXIT
trap shutdown INT TERM

start_champion
if [[ "$fast_enabled" == "1" ]]; then start_fast; fi
write_plane_status

while true; do
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
