#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any


def run_git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def build_snapshot(root: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = load(manifest_path)
    tracked = [x for x in run_git(root, "ls-files").splitlines() if x]
    required = [str(x) for x in manifest.get("required_surfaces", [])]
    missing = [rel for rel in required if not (root / rel).exists()]
    if missing:
        raise ValueError("missing required project surfaces: " + ", ".join(missing))
    champion = load(root / str(manifest["live_champion_manifest"]))
    registry = load(root / str(manifest["scheduler_registry"]))
    directives_rel = str(manifest["operator_directives"])
    directives = load(root / directives_rel)
    receipt_rel = str(manifest.get("cutover_receipt") or "")
    receipt_path = root / receipt_rel if receipt_rel else None
    receipt = load(receipt_path) if receipt_path is not None and receipt_path.is_file() else None
    workflows = sorted(x for x in tracked if x.startswith(".github/workflows/") and x.endswith((".yml", ".yaml")))
    return {
        "schema_version": 6,
        "repository": manifest.get("repository"),
        "head": run_git(root, "rev-parse", "HEAD"),
        "branch": run_git(root, "branch", "--show-current"),
        "tracked_file_count": len(tracked),
        "tracked_files": tracked,
        "top_level_counts": dict(sorted(Counter(x.split("/", 1)[0] for x in tracked).items())),
        "workflow_count": len(workflows),
        "workflows": workflows,
        "script_count": sum(x.startswith("scripts/") for x in tracked),
        "test_count": sum(x.startswith("tests/") for x in tracked),
        "required_surfaces": required,
        "live_champion": champion,
        "scheduler_count": len(registry.get("schedulers", [])),
        "runtime": manifest.get("runtime", {}),
        "grafana": manifest.get("grafana", {}),
        "cutover": manifest.get("cutover", {}),
        "cutover_receipt_path": receipt_rel or None,
        "cutover_receipt_present": receipt is not None,
        "cutover_receipt": receipt,
        "context_policy": manifest.get("context_policy", {}),
        "security": manifest.get("security", {}),
        "operator_directives_path": directives_rel,
        "operator_directives": directives,
    }


def render(snapshot: dict[str, Any]) -> str:
    champion = snapshot["live_champion"]
    state = "disabled" if champion.get("enabled") is False else f"V{champion.get('version')}"
    directives = snapshot["operator_directives"]
    cutover = snapshot.get("cutover") if isinstance(snapshot.get("cutover"), dict) else {}
    lines = [
        "# Project context snapshot", "",
        f"- repository: `{snapshot['repository']}`",
        f"- head: `{snapshot['head']}`",
        f"- branch: `{snapshot['branch'] or '<detached>'}`",
        f"- tracked files: **{snapshot['tracked_file_count']}**",
        f"- workflows: **{snapshot['workflow_count']}**",
        f"- schedulers: **{snapshot['scheduler_count']}**",
        f"- operational champion: **{state}**",
        f"- target champion: **V{cutover.get('target_version', '?')}**",
        f"- control-plane state: `{cutover.get('current_state')}`",
        f"- required cutover sequence: `{cutover.get('required_sequence')}`",
        f"- cutover receipt: `{snapshot.get('cutover_receipt_path')}` present={snapshot.get('cutover_receipt_present')}",
        f"- operator directive epoch: `{directives.get('directive_epoch')}`",
        "", "## Current priority order", "",
    ]
    lines.extend(f"{i}. {x}" for i, x in enumerate(directives.get("current_priority_order", []), 1))
    lines += ["", "## Repository surfaces", "", "| Surface | Tracked files |", "|---|---:|"]
    lines.extend(f"| `{name}` | {count} |" for name, count in snapshot["top_level_counts"].items())
    return "\n".join(lines) + "\n"


def main() -> int:
    p = argparse.ArgumentParser(); p.add_argument("--root", default="."); p.add_argument("--manifest", default="config/project_context.json"); p.add_argument("--output-json", default="project-context.json"); p.add_argument("--output-markdown", default="project-context.md")
    a = p.parse_args(); root = Path(a.root).resolve(); manifest = root / a.manifest
    snapshot = build_snapshot(root, manifest)
    Path(a.output_json).write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    Path(a.output_markdown).write_text(render(snapshot), encoding="utf-8")
    print(json.dumps({"head": snapshot["head"], "tracked_file_count": snapshot["tracked_file_count"], "workflow_count": snapshot["workflow_count"], "scheduler_count": snapshot["scheduler_count"], "champion_enabled": snapshot["live_champion"].get("enabled"), "cutover_state": snapshot.get("cutover", {}).get("current_state"), "cutover_receipt_present": snapshot.get("cutover_receipt_present"), "operator_directive_epoch": snapshot["operator_directives"].get("directive_epoch")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
