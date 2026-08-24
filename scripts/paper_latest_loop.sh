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

exec /usr/bin/env bash "$loop" "$config" "$run_root"
