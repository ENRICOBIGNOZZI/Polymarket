#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"{path} must contain a JSON object")
    return value


def safe_rel(value: Any, prefix: str) -> str:
    raw = str(value or "")
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or ".." in path.parts or not raw.startswith(prefix):
        raise RuntimeError(f"unsafe {prefix} path: {raw!r}")
    return raw


def run(root: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(args, cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and proc.returncode:
        message = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"{' '.join(args)}: {message}")
    return proc.stdout.strip()


def validate_manifest(root: Path, manifest_path: Path, *, require_enabled: bool = True) -> dict[str, Any]:
    manifest = read_json(manifest_path)
    enabled = manifest.get("enabled") is True
    if require_enabled and not enabled:
        raise RuntimeError("live champion is disabled")
    if enabled:
        if manifest.get("version") != 7:
            raise RuntimeError("enabled champion must be V7")
        if manifest.get("paper_only") is not True:
            raise RuntimeError("enabled champion must be PAPER-only")
        if manifest.get("authenticated_execution") is not False:
            raise RuntimeError("authenticated execution must remain disabled")
        if manifest.get("deployment_ref") != "paper-validated":
            raise RuntimeError("enabled champion must deploy only from paper-validated")
        loop_rel = safe_rel(manifest.get("loop"), "scripts/")
        config_rel = safe_rel(manifest.get("config"), "config/")
        run_rel = safe_rel(manifest.get("run_root"), "runs/")
        if not (root / loop_rel).is_file():
            raise RuntimeError(f"V7 loop missing: {loop_rel}")
        if not (root / config_rel).is_file():
            raise RuntimeError(f"V7 config missing: {config_rel}")
        cfg = read_json(root / config_rel)
        if cfg.get("paper_only") is not True:
            raise RuntimeError("V7 config must be PAPER-only")
        if cfg.get("authenticated_execution") is not False:
            raise RuntimeError("V7 config must disable authenticated execution")
        if "v7" not in cfg and cfg.get("engine_version") != 7 and cfg.get("version") != 7:
            raise RuntimeError("V7 config lacks an explicit V7 contract")
        manifest = {**manifest, "loop": loop_rel, "config": config_rel, "run_root": run_rel}
    return manifest


def validate_git(root: Path, expected_sha: str) -> dict[str, str]:
    if len(expected_sha) != 40 or any(ch not in "0123456789abcdef" for ch in expected_sha.lower()):
        raise RuntimeError("expected SHA must be exactly 40 hexadecimal characters")
    head = run(root, "git", "rev-parse", "HEAD")
    validated = run(root, "git", "rev-parse", "origin/paper-validated")
    main = run(root, "git", "rev-parse", "origin/main")
    if head != expected_sha:
        raise RuntimeError(f"server HEAD mismatch: {head} != {expected_sha}")
    if validated != expected_sha:
        raise RuntimeError(f"paper-validated mismatch: {validated} != {expected_sha}")
    run(root, "git", "merge-base", "--is-ancestor", expected_sha, main)
    return {"head": head, "paper_validated": validated, "main": main}


def pid_alive(pid: Any) -> bool:
    try:
        number = int(pid)
        if number <= 0:
            return False
        os.kill(number, 0)
        return True
    except (TypeError, ValueError, ProcessLookupError, PermissionError, OSError):
        return False


def http_get(url: str, timeout: float = 4.0) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "polymarket-v7-health/1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"GET {url}: {exc}") from exc


def process_count(pattern: str) -> int:
    proc = subprocess.run(["pgrep", "-af", pattern], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if proc.returncode not in (0, 1):
        raise RuntimeError("pgrep failed while checking single-writer process ownership")
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    return len(lines)


def runtime_health(root: Path, manifest: dict[str, Any], max_age_seconds: float, require_monitoring: bool) -> dict[str, Any]:
    run_root = root / str(manifest["run_root"])
    state_root = run_root if (run_root / "runtime_status.json").is_file() else run_root / "execution"
    supervisor = read_json(run_root / "v7_supervisor.json")
    timestamp = float(supervisor.get("timestamp") or 0.0)
    supervisor_age = max(0.0, time.time() - timestamp) if timestamp else float("inf")
    if supervisor.get("execution_alive") is not True or supervisor.get("shadow_alive") is not True:
        raise RuntimeError("V7 supervisor reports a dead child")
    if supervisor_age > max_age_seconds:
        raise RuntimeError(f"V7 supervisor is stale: {supervisor_age:.1f}s")
    if not pid_alive(supervisor.get("execution_pid")) or not pid_alive(supervisor.get("shadow_pid")):
        raise RuntimeError("V7 supervisor child PID is not alive")

    loop_rel = str(manifest["loop"])
    count = process_count(loop_rel)
    if count != 1:
        raise RuntimeError(f"single-writer violation: {loop_rel} process count={count}")

    contract = subprocess.run(
        [sys.executable, str(root / "scripts/runtime_contract_health.py"), "--manifest", str(root / "config/live_champion.json"), "--repository-root", str(root), "--max-age-seconds", str(max_age_seconds)],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if contract.returncode:
        raise RuntimeError("runtime contract health failed: " + (contract.stderr.strip() or contract.stdout.strip()))

    status = read_json(state_root / "runtime_status.json")
    if status.get("version") != 7 or status.get("paper_only") is not True or status.get("authenticated_execution") is not False:
        raise RuntimeError("runtime status violates V7 PAPER boundary")

    monitoring: dict[str, Any] = {"required": require_monitoring}
    if require_monitoring:
        code, body = http_get("http://127.0.0.1:9108/healthz")
        if code != 200:
            raise RuntimeError(f"V7 exporter unhealthy: HTTP {code} {body[:200]}")
        code, metrics = http_get("http://127.0.0.1:9108/metrics")
        if code != 200 or "polymarket_v7_runtime_alive 1" not in metrics:
            raise RuntimeError("V7 exporter metrics do not prove runtime_alive=1")
        if "polymarket_v7_runtime_authenticated_execution 0" not in metrics:
            raise RuntimeError("V7 exporter does not prove authenticated_execution=0")
        pcode, _ = http_get("http://127.0.0.1:9090/-/ready")
        if pcode != 200:
            raise RuntimeError("Prometheus is not ready")
        gcode, _ = http_get("http://127.0.0.1:3000/api/health")
        if gcode != 200:
            raise RuntimeError("Grafana is not healthy")
        dcode, _ = http_get("http://127.0.0.1:3000/api/dashboards/uid/polymarket-v7")
        if dcode != 200:
            raise RuntimeError("canonical V7 Grafana dashboard is missing")
        monitoring.update({"exporter": True, "prometheus": True, "grafana": True, "dashboard": "polymarket-v7"})

    return {
        "run_root": str(manifest["run_root"]),
        "supervisor_age_seconds": supervisor_age,
        "single_writer_process_count": count,
        "equity": status.get("equity"),
        "pnl": status.get("pnl"),
        "realized_pnl": status.get("realized_pnl"),
        "drawdown": status.get("drawdown"),
        "gross_exposure": status.get("gross_exposure"),
        "monitoring": monitoring,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail-closed exact-SHA health contract for the canonical V7 PAPER server")
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--manifest", type=Path, default=Path("config/live_champion.json"))
    parser.add_argument("--expected-sha")
    parser.add_argument("--max-age-seconds", type=float, default=180.0)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--require-monitoring", action="store_true")
    parser.add_argument("--allow-disabled", action="store_true")
    args = parser.parse_args()

    root = args.repository_root.resolve()
    manifest_path = args.manifest if args.manifest.is_absolute() else root / args.manifest
    try:
        manifest = validate_manifest(root, manifest_path, require_enabled=not args.allow_disabled)
        if manifest.get("enabled") is not True:
            print(json.dumps({"ok": True, "state": "no_champion", "enabled": False}, sort_keys=True))
            return 0
        git_state = validate_git(root, args.expected_sha) if args.expected_sha else {}
        result: dict[str, Any] = {"ok": True, "state": "preflight", "version": 7, "paper_only": True, "authenticated_execution": False, "git": git_state}
        if not args.preflight_only:
            result["runtime"] = runtime_health(root, manifest, args.max_age_seconds, args.require_monitoring)
            result["state"] = "healthy"
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
