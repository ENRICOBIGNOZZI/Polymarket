#!/usr/bin/env bash
set -euo pipefail

labels=(
  com.polymarket.awake
  com.polymarket.paper
  com.polymarket.exporter
  com.polymarket.prometheus
  com.polymarket.grafana
)

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

[[ $# -eq 1 ]] || usage
action="$1"

case "$action" in
  restart)
    for label in "${labels[@]}"; do
      /bin/launchctl kickstart -k "system/$label"
    done
    wait_monitoring
    ;;
  status)
    for label in "${labels[@]}"; do
      echo "===== $label ====="
      /bin/launchctl print "system/$label" | sed -n '1,30p'
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
