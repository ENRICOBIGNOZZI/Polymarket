#!/usr/bin/env bash
# Minimal shared child-registration boundary for the canonical V7 launcher.

v7_register_child() {
  local child_pid="${1:-}"
  if [[ ! "$child_pid" =~ ^[1-9][0-9]*$ ]]; then
    echo "invalid V7 child PID" >&2
    return 64
  fi
  pids+=("$child_pid")
  fatal_pids+=("$child_pid")
}

v7_register_optional_child() {
  local child_pid="${1:-}"
  if [[ ! "$child_pid" =~ ^[1-9][0-9]*$ ]]; then
    echo "invalid V7 optional child PID" >&2
    return 64
  fi
  pids+=("$child_pid")
}

v7_assert_registered_child_count() {
  local expected="${1:-}"
  if [[ ! "$expected" =~ ^[0-9]+$ ]] || [[ "${#pids[@]}" -ne "$expected" ]]; then
    echo "V7 child registration mismatch expected=$expected actual=${#pids[@]}" >&2
    return 65
  fi
}

# Launch one runtime process in an explicit resource class. On Linux/London the
# classes are CPU-affined with taskset. Research/analytics are never valid
# classes here: they run on the research plane, not under the London launcher.
v7_exec_class() {
  local class="${1:-}"
  shift || true
  local cpuset="" nice_value="0"
  case "$class" in
    HOT_PATH)
      cpuset="${PM_V7_HOT_CPUSET:-}"
      nice_value="${PM_V7_HOT_NICE:-0}"
      ;;
    COLLECTOR)
      cpuset="${PM_V7_COLLECTOR_CPUSET:-}"
      nice_value="${PM_V7_COLLECTOR_NICE:-5}"
      ;;
    LATENCY_OBSERVER)
      cpuset="${PM_V7_LATENCY_CPUSET:-${PM_V7_COLLECTOR_CPUSET:-}}"
      nice_value="${PM_V7_LATENCY_NICE:-0}"
      ;;
    CONTROL)
      cpuset="${PM_V7_CONTROL_CPUSET:-}"
      nice_value="${PM_V7_CONTROL_NICE:-3}"
      ;;
    *)
      echo "invalid V7 runtime resource class: $class" >&2
      return 66
      ;;
  esac
  if [[ "$(uname -s)" == "Linux" ]] && command -v taskset >/dev/null 2>&1 && [[ -n "$cpuset" ]]; then
    if [[ "$nice_value" =~ ^[0-9]+$ ]] && [[ "$nice_value" -gt 0 ]]; then
      exec nice -n "$nice_value" taskset -c "$cpuset" "$@"
    fi
    exec taskset -c "$cpuset" "$@"
  fi
  if [[ "$class" != "HOT_PATH" ]] && [[ -x /usr/sbin/taskpolicy ]]; then
    exec /usr/sbin/taskpolicy -b nice -n "$nice_value" "$@"
  fi
  if [[ "$nice_value" =~ ^[0-9]+$ ]] && [[ "$nice_value" -gt 0 ]]; then
    exec nice -n "$nice_value" "$@"
  fi
  exec "$@"
}

# Synchronous variant used by bounded control loops that invoke one command per
# iteration and must retain the parent shell for the next iteration.
v7_run_class() {
  local class="${1:-}"
  shift || true
  local cpuset="" nice_value="0"
  case "$class" in
    HOT_PATH) cpuset="${PM_V7_HOT_CPUSET:-}"; nice_value="${PM_V7_HOT_NICE:-0}" ;;
    COLLECTOR) cpuset="${PM_V7_COLLECTOR_CPUSET:-}"; nice_value="${PM_V7_COLLECTOR_NICE:-5}" ;;
    LATENCY_OBSERVER) cpuset="${PM_V7_LATENCY_CPUSET:-${PM_V7_COLLECTOR_CPUSET:-}}"; nice_value="${PM_V7_LATENCY_NICE:-0}" ;;
    CONTROL) cpuset="${PM_V7_CONTROL_CPUSET:-}"; nice_value="${PM_V7_CONTROL_NICE:-3}" ;;
    *) echo "invalid V7 runtime resource class: $class" >&2; return 66 ;;
  esac
  if [[ "$(uname -s)" == "Linux" ]] && command -v taskset >/dev/null 2>&1 && [[ -n "$cpuset" ]]; then
    if [[ "$nice_value" =~ ^[0-9]+$ ]] && [[ "$nice_value" -gt 0 ]]; then
      nice -n "$nice_value" taskset -c "$cpuset" "$@"
    else
      taskset -c "$cpuset" "$@"
    fi
    return $?
  fi
  if [[ "$class" != "HOT_PATH" ]] && [[ -x /usr/sbin/taskpolicy ]]; then
    /usr/sbin/taskpolicy -b nice -n "$nice_value" "$@"
    return $?
  fi
  if [[ "$nice_value" =~ ^[0-9]+$ ]] && [[ "$nice_value" -gt 0 ]]; then
    nice -n "$nice_value" "$@"
  else
    "$@"
  fi
}

# Reproduce `git hash-object <file>` without requiring Git or repository history
# in the minimal London image.
v7_blob_hash() {
  local path="${1:?file required}"
  python3 - "$path" <<'PY'
import hashlib,sys
raw=open(sys.argv[1],'rb').read()
h=hashlib.sha1();h.update(f"blob {len(raw)}\0".encode());h.update(raw);print(h.hexdigest())
PY
}
