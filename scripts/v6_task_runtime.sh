#!/usr/bin/env bash

# Bash 3.2-compatible task primitives for the V6 paper loop.  The caller owns
# task cadence and PID slots; this helper owns atomic status writes, child
# forwarding, and bounded shutdown.  It deliberately has no authenticated
# execution surface.

V6_TASK_STARTED_PID=0
v6_task_child_pid=0
v6_task_name=""
v6_task_started=0

v6_task_write_status() {
  local task="$1" state="$2" started="$3" finished="$4" last_rc="$5"
  local output tmp
  output="$V6_TASK_STATUS_DIR/${task}.json"
  tmp="${output}.tmp.$$.$RANDOM"
  printf '{"schema_version":"polymarket_v6_task_status_v1","task":"%s","state":"%s","started":%s,"finished":%s,"last_rc":%s}\n' \
    "$task" "$state" "$started" "$finished" "$last_rc" >"$tmp"
  mv -f "$tmp" "$output"
}

v6_task_stop_child() {
  local attempt
  if ((v6_task_child_pid <= 0)); then
    return 0
  fi
  if kill -0 "$v6_task_child_pid" 2>/dev/null; then
    kill -TERM "$v6_task_child_pid" 2>/dev/null || true
    attempt=0
    while kill -0 "$v6_task_child_pid" 2>/dev/null && ((attempt < 30)); do
      sleep 0.1
      attempt=$((attempt + 1))
    done
    if kill -0 "$v6_task_child_pid" 2>/dev/null; then
      kill -KILL "$v6_task_child_pid" 2>/dev/null || true
    fi
  fi
  wait "$v6_task_child_pid" 2>/dev/null || true
  v6_task_child_pid=0
}

v6_task_abort() {
  local signal_rc="$1" finished
  trap - HUP INT TERM
  v6_task_stop_child
  finished="$(date +%s)"
  v6_task_write_status "$v6_task_name" "terminated" "$v6_task_started" "$finished" "$signal_rc"
  exit "$signal_rc"
}

v6_task_run_child() {
  local rc
  "$@" &
  v6_task_child_pid=$!
  if wait "$v6_task_child_pid"; then
    rc=0
  else
    rc=$?
  fi
  v6_task_child_pid=0
  return "$rc"
}

v6_task_start() {
  local task="$1" started="$2" body="$3"
  V6_TASK_STARTED_PID=0
  v6_task_write_status "$task" "running" "$started" "null" "null"
  (
    # Never inherit the loop's EXIT cleanup: a completed scanner must not stop
    # the recorder, broker, proxy, or allocator copied into this subshell.
    trap - EXIT
    set +e
    v6_task_name="$task"
    v6_task_started="$started"
    v6_task_child_pid=0
    trap 'v6_task_abort 129' HUP
    trap 'v6_task_abort 130' INT
    trap 'v6_task_abort 143' TERM
    "$body"
    rc=$?
    trap - HUP INT TERM
    finished="$(date +%s)"
    if ((rc == 0)); then
      state="succeeded"
    else
      state="failed"
    fi
    v6_task_write_status "$task" "$state" "$started" "$finished" "$rc"
    exit "$rc"
  ) &
  V6_TASK_STARTED_PID=$!
}

v6_task_is_running() {
  local pid="${1:-0}"
  ((pid > 0)) && kill -0 "$pid" 2>/dev/null
}

v6_task_reap_if_finished() {
  local pid="${1:-0}"
  if ((pid <= 0)) || kill -0 "$pid" 2>/dev/null; then
    return 1
  fi
  wait "$pid" 2>/dev/null || true
  return 0
}

v6_task_terminate_pids() {
  local pid attempt alive
  for pid in "$@"; do
    if ((pid > 0)) && kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  attempt=0
  while ((attempt < 50)); do
    alive=0
    for pid in "$@"; do
      if ((pid > 0)) && kill -0 "$pid" 2>/dev/null; then
        alive=1
      fi
    done
    ((alive == 0)) && break
    sleep 0.1
    attempt=$((attempt + 1))
  done
  for pid in "$@"; do
    if ((pid > 0)) && kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || true
    fi
  done
  for pid in "$@"; do
    if ((pid > 0)); then
      wait "$pid" 2>/dev/null || true
    fi
  done
}
