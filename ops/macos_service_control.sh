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

[[ $# -eq 1 ]] || usage
action="$1"

case "$action" in
  restart)
    for label in "${labels[@]}"; do
      /bin/launchctl kickstart -k "system/$label"
    done
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
    ;;
  *) usage ;;
esac
