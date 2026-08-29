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

# Own the *entire* manifest-selected paper runtime with one inherited advisory
# lock.  Child-level locks remain useful, but they are too late to prevent two
# wrappers from launching competing recorder/model writers.  Re-exec through a
# tiny fcntl launcher so the lock fd survives for this process lifetime.
singleton_lock="$run_root/runtime_owner.lock"
handoff_marker="$run_root/runtime_handoff.request"

runtime_tree_pids() {
  python3 - "$1" <<'PY'
import subprocess
import sys

root = int(sys.argv[1])
rows = []
for line in subprocess.check_output(["/bin/ps", "-axo", "pid=,ppid="], text=True).splitlines():
    fields = line.split()
    if len(fields) != 2:
        continue
    try:
        rows.append((int(fields[0]), int(fields[1])))
    except ValueError:
        continue
children = {}
for pid, parent in rows:
    children.setdefault(parent, []).append(pid)
out, stack, seen = [], [root], {root}
while stack:
    parent = stack.pop()
    for child in children.get(parent, []):
        if child in seen:
            continue
        seen.add(child)
        out.append(child)
        stack.append(child)
print("\\n".join(str(pid) for pid in out + [root]))
PY
}

same_repository_runtime_owner() {
  local pid="$1" command_line cwd
  [[ "$pid" =~ ^[1-9][0-9]*$ && "$pid" != "$$" ]] || return 1
  command_line="$(/bin/ps -o command= -p "$pid" 2>/dev/null || true)"
  [[ "$command_line" == *"paper_latest_loop.sh"* || "$command_line" == *"runtime_singleton_launcher.py"* ]] || return 1
  [[ "$command_line" == *"$ROOT/scripts/paper_latest_loop.sh"* || "$command_line" == *"$ROOT/scripts/runtime_singleton_launcher.py"* ]] && return 0
  command -v lsof >/dev/null 2>&1 || return 1
  cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | /usr/bin/sed -n 's/^n//p' | /usr/bin/head -n 1)"
  [[ "$cwd" == "$ROOT" ]]
}

same_repository_runtime_lock_holder() {
  local pid="$1" cwd
  [[ "$pid" =~ ^[1-9][0-9]*$ && "$pid" != "$$" ]] || return 1
  command -v lsof >/dev/null 2>&1 || return 1
  cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | /usr/bin/sed -n 's/^n//p' | /usr/bin/head -n 1)"
  [[ "$cwd" == "$ROOT" ]]
}

runtime_lock_is_free() {
  python3 - "$singleton_lock" <<'PY'
import fcntl
import os
import sys

fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(1)
finally:
    os.close(fd)
PY
}

wait_for_runtime_lock_to_release() {
  local attempts="$1" attempt
  for ((attempt=1; attempt<=attempts; ++attempt)); do
    runtime_lock_is_free && return 0
    sleep 0.2
  done
  return 1
}

reap_requested_stale_runtime_owner() {
  [[ -f "$handoff_marker" ]] || return 0
  command -v lsof >/dev/null 2>&1 || { echo "fatal: runtime handoff requires lsof" >&2; return 1; }
  runtime_lock_is_free && return 0
  local owner pid target
  owner="$(/usr/bin/tr -dc '0-9' < "$singleton_lock" 2>/dev/null || true)"
  [[ "$owner" =~ ^[1-9][0-9]*$ && "$owner" != "$$" ]] || {
    echo "fatal: runtime handoff found a lock without a usable owner pid" >&2
    return 1
  }
  local holders=()
  while IFS= read -r pid; do
    [[ "$pid" =~ ^[1-9][0-9]*$ && "$pid" != "$$" ]] && holders+=("$pid")
  done < <(lsof -t "$singleton_lock" 2>/dev/null || true)
  ((${#holders[@]})) || {
    echo "fatal: runtime handoff lock owner disappeared before ownership could be verified" >&2
    return 1
  }
  local owner_holds_lock=0 owner_is_alive=0
  for pid in "${holders[@]}"; do
    [[ "$pid" == "$owner" ]] && owner_holds_lock=1
  done
  kill -0 "$owner" 2>/dev/null && owner_is_alive=1
  if (( owner_holds_lock == 1 )); then
    same_repository_runtime_owner "$owner" || {
      echo "fatal: runtime handoff refused unknown lock owner pid=$owner" >&2
      return 1
    }
    echo "stale_runtime_owner_reaped=$owner" >&2
    while IFS= read -r target; do
      [[ "$target" =~ ^[1-9][0-9]*$ ]] && kill -TERM "$target" 2>/dev/null || true
    done < <(runtime_tree_pids "$owner")
  elif (( owner_is_alive == 0 )); then
    # A legacy wrapper can die while a child retains its inherited fd.  A dead
    # recorded owner plus actual lsof holders is sufficient only after every
    # holder proves the same repository cwd; never infer ownership by name.
    for pid in "${holders[@]}"; do
      same_repository_runtime_lock_holder "$pid" || {
        echo "fatal: runtime handoff refused unknown orphan lock holder pid=$pid" >&2
        return 1
      }
    done
    for pid in "${holders[@]}"; do
      echo "stale_runtime_orphan_lock_holder_reaped=$pid" >&2
      kill -TERM "$pid" 2>/dev/null || true
    done
  else
    echo "fatal: runtime handoff owner pid=$owner is alive but no longer holds the singleton lock" >&2
    return 1
  fi
  wait_for_runtime_lock_to_release 25 && return 0

  # Older wrappers deliberately inherited the singleton descriptor.  If one
  # resistant descendant retained it after the verified owner's tree received
  # TERM, terminate only lock holders whose cwd proves they belong to this
  # repository.  Anything else leaves startup fail-closed.
  holders=()
  while IFS= read -r pid; do
    [[ "$pid" =~ ^[1-9][0-9]*$ && "$pid" != "$$" ]] && holders+=("$pid")
  done < <(lsof -t "$singleton_lock" 2>/dev/null || true)
  ((${#holders[@]})) || {
    echo "fatal: requested paper runtime handoff retained an unverifiable lock" >&2
    return 1
  }
  for pid in "${holders[@]}"; do
    same_repository_runtime_lock_holder "$pid" || {
      echo "fatal: runtime handoff refused unknown lock holder pid=$pid" >&2
      return 1
    }
  done
  echo "stale_runtime_owner_killed=$owner" >&2
  for pid in "${holders[@]}"; do
    kill -KILL "$pid" 2>/dev/null || true
  done
  wait_for_runtime_lock_to_release 25 || {
    echo "fatal: requested paper runtime handoff retained stale lock ownership" >&2
    return 1
  }
}

if [[ "${POLYMARKET_RUNTIME_SINGLETON_HELD:-0}" != "1" ]]; then
  reap_requested_stale_runtime_owner || exit 1
  exec python3 "$ROOT/scripts/runtime_singleton_launcher.py" \
    --lock "$singleton_lock" -- \
    env POLYMARKET_RUNTIME_SINGLETON_HELD=1 \
    /usr/bin/env bash "$ROOT/scripts/paper_latest_loop.sh" "$@"
fi
rm -f "$handoff_marker"

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

start_champion() {
  # Let the V6 child notice an abrupt launchd-wrapper death and retire its
  # broker/proxy children before a replacement runtime is allowed to own them.
  POLYMARKET_RUNTIME_PARENT_PID="$$" /usr/bin/env bash "$loop" "$config" "$run_root" &
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
  # macOS ships Bash 3.2, which has no BASHPID. The singleton lock already
  # excludes a second runtime; the shell PID is a portable per-wrapper suffix.
  local tmp="$path.tmp.${BASHPID:-$$}"
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
