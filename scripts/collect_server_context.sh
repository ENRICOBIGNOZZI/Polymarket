#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

OUTPUT_DIR="${1:-runs/server-context}"
CONFIG_PATH="${SERVER_RUNTIME_CONFIG:-config/server_runtime.json}"
IDENTITY_FILE="${SSH_IDENTITY_FILE:-$HOME/.ssh/polymarket_deploy}"

[[ -f "$CONFIG_PATH" ]] || {
  echo "fatal: server runtime config not found: $CONFIG_PATH" >&2
  exit 1
}

readarray -t defaults < <(
  python3 - "$CONFIG_PATH" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
required = {
    "schema_version",
    "host_default",
    "user_default",
    "port_default",
    "repo_root_default",
    "deployment_ref",
    "tailscale_auth_secret",
    "ssh_private_key_secret",
}
missing = sorted(required.difference(data))
if missing:
    raise SystemExit("server runtime config missing keys: " + ", ".join(missing))
if data["schema_version"] != 1:
    raise SystemExit("unsupported server runtime schema")
print(data["host_default"])
print(data["user_default"])
print(data["port_default"])
print(data["repo_root_default"])
print(data["deployment_ref"])
PY
)

SERVER_HOST="${SERVER_HOST:-${defaults[0]}}"
SERVER_USER="${SERVER_USER:-${defaults[1]}}"
SERVER_PORT="${SERVER_PORT:-${defaults[2]}}"
SERVER_REPO_ROOT="${SERVER_REPO_ROOT:-${defaults[3]}}"
SERVER_DEPLOYMENT_REF="${SERVER_DEPLOYMENT_REF:-${defaults[4]}}"

[[ "$SERVER_PORT" =~ ^[0-9]+$ ]] || {
  echo "fatal: SERVER_PORT must be numeric" >&2
  exit 1
}
[[ "$SERVER_REPO_ROOT" = /* ]] || {
  echo "fatal: SERVER_REPO_ROOT must be absolute" >&2
  exit 1
}
[[ -s "$IDENTITY_FILE" ]] || {
  echo "fatal: SSH identity missing: $IDENTITY_FILE" >&2
  exit 1
}

mkdir -p "$OUTPUT_DIR"
chmod 700 "$OUTPUT_DIR"

SSH=(
  ssh
  -i "$IDENTITY_FILE"
  -o IdentitiesOnly=yes
  -o BatchMode=yes
  -o StrictHostKeyChecking=yes
  -o ConnectTimeout=10
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=3
  -p "$SERVER_PORT"
  "$SERVER_USER@$SERVER_HOST"
)

"${SSH[@]}" python3 - "$SERVER_REPO_ROOT" "$SERVER_DEPLOYMENT_REF" <<'PY' > "$OUTPUT_DIR/server_context.remote.json"
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

repo = Path(sys.argv[1]).expanduser().resolve()
deployment_ref = sys.argv[2]


def run(*command: str, timeout: int = 20) -> dict[str, object]:
    try:
        completed = subprocess.run(
            command,
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "command": list(command),
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip()[-8000:],
            "stderr": completed.stderr.strip()[-4000:],
        }
    except Exception as exc:  # evidence collection must report, not conceal, failures
        return {
            "command": list(command),
            "returncode": 255,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
        }

if not repo.is_dir():
    raise SystemExit(f"repository root does not exist: {repo}")

fetch = run("git", "fetch", "--quiet", "origin", "main", deployment_ref)
head = run("git", "rev-parse", "HEAD")
main = run("git", "rev-parse", "origin/main")
validated = run("git", "rev-parse", f"origin/{deployment_ref}")

system = platform.system()
if system == "Darwin":
    services = run("sudo", "-n", "/usr/local/sbin/polymarket-service-control", "status")
elif system == "Linux":
    services = run(
        "systemctl",
        "--no-pager",
        "--full",
        "status",
        "polymarket-paper.service",
        "polymarket-monitoring.service",
    )
else:
    services = {
        "command": ["service-status"],
        "returncode": 2,
        "stdout": "",
        "stderr": f"unsupported OS for service inspection: {system}",
    }

health = {
    "runtime_exporter": run("curl", "-fsS", "--max-time", "5", "http://127.0.0.1:9108/healthz"),
    "prometheus": run("curl", "-fsS", "--max-time", "5", "http://127.0.0.1:9090/-/ready"),
    "grafana": run("curl", "-fsS", "--max-time", "5", "http://127.0.0.1:3000/api/health"),
}

names = {
    "fast_arb_status.json",
    "fast_arb_opportunities.csv",
    "fast_arb_latency.csv",
    "runtime_planes.csv",
    "global_risk_state.json",
    "runtime_status.json",
    "walk_forward.json",
    "action_report.json",
}
files: list[dict[str, object]] = []
runs = repo / "runs"
if runs.is_dir():
    for path in runs.rglob("*"):
        if not path.is_file() or path.name not in names:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append(
            {
                "path": str(path.relative_to(repo)),
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
                "age_seconds": max(0, int(time.time() - stat.st_mtime)),
            }
        )
files.sort(key=lambda item: (item["mtime"], item["path"]), reverse=True)

result = {
    "schema_version": 1,
    "collected_at": int(time.time()),
    "repo_root": str(repo),
    "deployment_ref": deployment_ref,
    "os": system,
    "hostname": platform.node(),
    "git_fetch": fetch,
    "server_head": head,
    "origin_main": main,
    "origin_paper_validated": validated,
    "services": services,
    "health": health,
    "evidence_files": files[:500],
}
print(json.dumps(result, indent=2, sort_keys=True))
PY

python3 - "$OUTPUT_DIR/server_context.remote.json" "$OUTPUT_DIR/server_context.json" \
  "$SERVER_HOST" "$SERVER_USER" "$SERVER_PORT" "$SERVER_REPO_ROOT" <<'PY'
import json
import sys
from pathlib import Path

source, target, host, user, port, repo = sys.argv[1:]
data = json.loads(Path(source).read_text(encoding="utf-8"))
data.update(
    {
        "server_host": host,
        "server_user": user,
        "server_port": int(port),
        "configured_repo_root": repo,
        "transport": "tailscale+ssh",
        "ssh_identity_source": "POLYMARKET_SERVER_SSH_KEY",
        "tailscale_auth_source": "TS_AUTHKEY",
    }
)
Path(target).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
rm -f "$OUTPUT_DIR/server_context.remote.json"

# Stream only compact, explicitly named operational evidence. No credentials,
# wallet material or arbitrary runtime files are copied from the server.
"${SSH[@]}" python3 - "$SERVER_REPO_ROOT" <<'PY' > "$OUTPUT_DIR/server_fast_evidence.tar.gz"
from __future__ import annotations

import io
import os
import sys
import tarfile
import time
from pathlib import Path

repo = Path(sys.argv[1]).expanduser().resolve()
runs = repo / "runs"
allowed = {
    "fast_arb_status.json",
    "fast_arb_opportunities.csv",
    "fast_arb_latency.csv",
    "runtime_planes.csv",
    "global_risk_state.json",
    "runtime_status.json",
    "walk_forward.json",
    "action_report.json",
}
cutoff = time.time() - 72 * 3600
candidates: list[Path] = []
if runs.is_dir():
    for path in runs.rglob("*"):
        if not path.is_file() or path.name not in allowed:
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                candidates.append(path)
        except OSError:
            continue
candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as archive:
    for path in candidates[:500]:
        archive.add(path, arcname=str(path.relative_to(repo)), recursive=False)
PY

mkdir -p "$OUTPUT_DIR/server_files"
tar -xzf "$OUTPUT_DIR/server_fast_evidence.tar.gz" -C "$OUTPUT_DIR/server_files"
rm -f "$OUTPUT_DIR/server_fast_evidence.tar.gz"

python3 - "$OUTPUT_DIR/server_context.json" "$OUTPUT_DIR/server_context.md" <<'PY'
import json
import sys
from pathlib import Path

source, target = map(Path, sys.argv[1:])
data = json.loads(source.read_text(encoding="utf-8"))
health = data.get("health", {})

def rc(name: str) -> object:
    return health.get(name, {}).get("returncode", "missing")

lines = [
    "# Remote Polymarket server context",
    "",
    f"- transport: `{data.get('transport')}`",
    f"- endpoint: `{data.get('server_user')}@{data.get('server_host')}:{data.get('server_port')}`",
    f"- repository: `{data.get('configured_repo_root')}`",
    f"- deployment ref: `{data.get('deployment_ref')}`",
    f"- server OS: `{data.get('os')}`",
    f"- server hostname: `{data.get('hostname')}`",
    f"- discovered evidence files: {len(data.get('evidence_files', []))}",
    f"- runtime exporter health rc: `{rc('runtime_exporter')}`",
    f"- Prometheus health rc: `{rc('prometheus')}`",
    f"- Grafana health rc: `{rc('grafana')}`",
    "",
    "Credential values are never persisted. The workflow receives only the Tailscale and SSH secrets at runtime.",
]
target.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

cat "$OUTPUT_DIR/server_context.md"
