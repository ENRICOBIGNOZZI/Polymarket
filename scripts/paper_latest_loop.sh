#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Select the numerically newest committed paper_vN loop that has a matching config.
best_version=""
best_script=""
for script in scripts/paper_v*_loop.sh; do
  [[ -f "$script" ]] || continue
  name="$(basename "$script")"
  version="${name#paper_v}"
  version="${version%_loop.sh}"
  [[ "$version" =~ ^[0-9]+$ ]] || continue
  [[ -f "config/paper_v${version}.json" ]] || continue
  if [[ -z "$best_version" || "$version" -gt "$best_version" ]]; then
    best_version="$version"
    best_script="$script"
  fi
done

if [[ -z "$best_script" ]]; then
  echo "fatal: no scripts/paper_vN_loop.sh with matching config/paper_vN.json" >&2
  exit 1
fi

config="${POLYMARKET_CONFIG:-$ROOT/config/paper_v${best_version}.json}"
run_root="${POLYMARKET_RUN_ROOT:-$ROOT/runs/paper_v${best_version}_live}"

mkdir -p "$run_root"
printf 'paper_latest version=%s script=%s config=%s run_root=%s\n' \
  "$best_version" "$best_script" "$config" "$run_root"

exec /usr/bin/env bash "$ROOT/$best_script" "$config" "$run_root"
