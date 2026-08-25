#!/usr/bin/env bash
set -euo pipefail

labels=(
  com.polymarket.awake
  com.polymarket.paper
  com.polymarket.exporter
  com.polymarket.prometheus
  com.polymarket.grafana
)

SERVICE_RESTART_ATTEMPTS="${POLYMARKET_SERVICE_RESTART_ATTEMPTS:-120}"

usage() {
  echo "usage: $0 {restart|status|stop|start}" >&2
  exit 2
}

wait_http() {
  local url="$1" name="$2" attempts="${3:-60}"
  local i
  for ((i=1; i<=attempts; ++i)); do
    if /usr/bin/curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      printf '%s_ready=1 attempts=%s\n' "$name" "$i"
      return 0
    fi
    /bin/sleep 1
  done
  printf '%s_ready=0 url=%s\n' "$name" "$url" >&2
  return 1
}

wait_monitoring() {
  wait_http http://127.0.0.1:9108/healthz exporter
  wait_http http://127.0.0.1:9090/-/ready prometheus
  wait_http http://127.0.0.1:3000/api/health grafana
  wait_http http://127.0.0.1:3000/api/search grafana_anonymous
}

print_safe_status() {
  local label="$1"
  /bin/launchctl print "system/$label" | /usr/bin/awk '
    /^[[:space:]]*(active count|path|state|program|pid|last exit code) = / { print }
  '
}

launchd_pid() {
  local label="$1"
  /bin/launchctl print "system/$label" 2>/dev/null | /usr/bin/awk '
    /^[[:space:]]*pid = [0-9]+$/ { print $3; exit }
  '
}

paper_workdir() {
  /usr/bin/plutil -extract WorkingDirectory raw -o - \
    /Library/LaunchDaemons/com.polymarket.paper.plist 2>/dev/null
}

legacy_paper_supervisors() {
  local workdir="$1"
  /bin/ps -axo pid=,ppid=,command= | /usr/bin/awk -v needle="$workdir/scripts/paper_v6_loop.sh" '
    $2 == 1 && index($0, needle) { print $1 }
  '
}

reap_legacy_paper_supervisors() {
  # Older `kickstart -k` deploys could SIGKILL the launchd-owned wrapper before
  # its TERM trap reaped the V6 supervisor.  Such an orphan is reparented to
  # launchd (PPID 1), keeps the broker lock, and makes the replacement
  # supervisor retry every five seconds.  Remove only exact deployed V6
  # supervisors; private validation roots and ordinary user shells do not
  # match this path+PPID predicate.
  local workdir pids remaining attempt
  workdir="$(paper_workdir || true)"
  [[ -n "$workdir" ]] || return 0
  pids="$(legacy_paper_supervisors "$workdir")"
  [[ -n "$pids" ]] || return 0
  printf 'paper_legacy_supervisors=%s action=term\n' "$(printf '%s\n' "$pids" | /usr/bin/tr '\n' ',')"
  while IFS= read -r pid; do
    [[ "$pid" =~ ^[0-9]+$ ]] && /bin/kill -TERM "$pid" 2>/dev/null || true
  done <<< "$pids"
  for ((attempt=1; attempt<=SERVICE_RESTART_ATTEMPTS; ++attempt)); do
    remaining="$(legacy_paper_supervisors "$workdir")"
    if [[ -z "$remaining" ]]; then
      printf 'paper_legacy_supervisors_reaped=1 attempts=%s\n' "$attempt"
      return 0
    fi
    /bin/sleep 0.25
  done
  printf 'paper_legacy_supervisors_reaped=0 remaining=%s\n' \
    "$(printf '%s\n' "$remaining" | /usr/bin/tr '\n' ',')" >&2
  return 1
}

restart_gracefully() {
  local label="$1" old_pid new_pid attempt
  old_pid="$(launchd_pid "$label" || true)"
  if [[ -z "$old_pid" ]]; then
    # A loaded KeepAlive job may be between processes.  Avoid a privileged or
    # forceful kickstart and let launchd materialize its replacement.
    for ((attempt=1; attempt<=SERVICE_RESTART_ATTEMPTS; ++attempt)); do
      new_pid="$(launchd_pid "$label" || true)"
      if [[ -n "$new_pid" ]]; then
        printf 'launchd_label=%s old_pid=none new_pid=%s graceful_restart=1 attempts=%s\n' \
          "$label" "$new_pid" "$attempt"
        return 0
      fi
      /bin/sleep 0.25
    done
    printf 'launchd_label=%s old_pid=none graceful_restart=0\n' "$label" >&2
    return 1
  fi

  # KeepAlive starts a replacement after the old principal exits.  SIGTERM is
  # intentional: the paper wrapper waits for its V6 child cleanup, whereas
  # `kickstart -k` bypasses both TERM traps and can strand paper writers.
  # All five daemons are installed with UserName=$USER, so the deploy account
  # can signal the principal directly without broadening its sudo authority.
  /bin/kill -TERM "$old_pid"
  for ((attempt=1; attempt<=SERVICE_RESTART_ATTEMPTS; ++attempt)); do
    new_pid="$(launchd_pid "$label" || true)"
    if [[ -n "$new_pid" && "$new_pid" != "$old_pid" ]]; then
      printf 'launchd_label=%s old_pid=%s new_pid=%s graceful_restart=1 attempts=%s\n' \
        "$label" "$old_pid" "$new_pid" "$attempt"
      return 0
    fi
    /bin/sleep 0.25
  done
  printf 'launchd_label=%s old_pid=%s graceful_restart=0\n' "$label" "$old_pid" >&2
  return 1
}

[[ $# -eq 1 ]] || usage
action="$1"

case "$action" in
  restart)
    [[ "$SERVICE_RESTART_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || {
      echo "POLYMARKET_SERVICE_RESTART_ATTEMPTS must be a positive integer" >&2
      exit 2
    }
    reap_legacy_paper_supervisors
    for label in "${labels[@]}"; do
      restart_gracefully "$label"
    done
    wait_monitoring
    ;;
  status)
    for label in "${labels[@]}"; do
      echo "===== $label ====="
      print_safe_status "$label"
    done
    ;;
  stop)
    for label in "${labels[@]}"; do
      /bin/launchctl kill SIGTERM "system/$label" 2>/dev/null || true
    done
    ;;
  start)
    for label in "${labels[@]}"; do
      /bin/launchctl kickstart "system/$label"
    done
    wait_monitoring
    ;;
  *) usage ;;
esac
