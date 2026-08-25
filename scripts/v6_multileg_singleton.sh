#!/usr/bin/env bash
set -euo pipefail

if (($# < 2)); then
  echo "usage: $0 RUN_ROOT COMMAND [ARGS...]" >&2
  exit 64
fi

run_root="$1"
shift
mkdir -p "$run_root"
lock_dir="$run_root/.multileg_broker.lock"
pid_file="$lock_dir/pid"

for attempt in $(seq 1 20); do
  if mkdir "$lock_dir" 2>/dev/null; then
    printf '%s\n' "$$" > "$pid_file"
    exec "$@"
  fi

  owner=""
  if [[ -r "$pid_file" ]]; then
    IFS= read -r owner < "$pid_file" || true
  fi
  if [[ "$owner" =~ ^[0-9]+$ ]] && kill -0 "$owner" 2>/dev/null; then
    echo "multileg broker already running for $run_root (pid=$owner)" >&2
    exit 75
  fi

  # A just-created lock can exist briefly before its owner PID is visible.
  # Wait before considering it stale so simultaneous supervisors cannot both
  # launch a broker. A stale lock is safe to remove because its recorded PID
  # is absent or no longer alive.
  if ((attempt < 20)); then
    sleep 0.1
    continue
  fi
  rm -rf "$lock_dir"
done

if ! mkdir "$lock_dir" 2>/dev/null; then
  echo "cannot acquire multileg broker singleton lock for $run_root" >&2
  exit 75
fi
printf '%s\n' "$$" > "$pid_file"
exec "$@"
