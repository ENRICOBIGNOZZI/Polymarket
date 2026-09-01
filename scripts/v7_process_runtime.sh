#!/usr/bin/env bash
# Minimal shared child-registration boundary for the canonical V7 launcher.

v7_register_child() {
  local child_pid="${1:-}"
  if [[ ! "$child_pid" =~ ^[1-9][0-9]*$ ]]; then
    echo "invalid V7 child PID" >&2
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
