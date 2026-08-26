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


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def build_snapshot(root: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    tracked = [line for line in run_git(root, "ls-files").splitlines() if line]
    required = [str(item) for item in manifest.get("required_surfaces", [])]
    missing: list[str] = []
    for rel in required:
        if not (root / rel).exists():
            missing.append(rel)

    top_level = Counter(path.split("/", 1)[0] for path in tracked)
    workflows = sorted(path for path in tracked if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")))
    tests = sorted(path for path in tracked if path.startswith("tests/"))
    scripts = sorted(path for path in tracked if path.startswith("scripts/"))

    champion_path = root / str(manifest["live_champion_manifest"])
    registry_path = root / str(manifest["scheduler_registry"])
    directives_rel = str(manifest.get("operator_directives") or "")
    directives_path = root / directives_rel if directives_rel else None
    champion = load_json(champion_path)
    registry = load_json(registry_path)
    if directives_path is None or not directives_path.is_file():
        raise ValueError("operator directives are required but missing")
    directives = load_json(directives_path)

    snapshot = {
        "schema_version": 2,
        "repository": manifest.get("repository"),
        "head": run_git(root, "rev-parse", "HEAD"),
        "branch": run_git(root, "branch", "--show-current"),
        "tracked_file_count": len(tracked),
        "tracked_files": tracked,
        "top_level_counts": dict(sorted(top_level.items())),
        "workflow_count": len(workflows),
        "workflows": workflows,
        "script_count": len(scripts),
        "test_count": len(tests),
        "required_surfaces": required,
        "missing_required_surfaces": missing,
        "live_champion": champion,
        "scheduler_count": len(registry.get("schedulers", [])),
        "runtime": manifest.get("runtime", {}),
        "grafana": manifest.get("grafana", {}),
        "context_policy": manifest.get("context_policy", {}),
        "security": manifest.get("security", {}),
        "operator_directives_path": directives_rel,
        "operator_directives": directives,
    }
    if missing:
        raise ValueError("missing required project surfaces: " + ", ".join(missing))
    return snapshot


def render_markdown(snapshot: dict[str, Any]) -> str:
    champion = snapshot.get("live_champion", {})
    grafana = snapshot.get("grafana", {})
    runtime = snapshot.get("runtime", {})
    directives = snapshot.get("operator_directives", {}) if isinstance(snapshot.get("operator_directives"), dict) else {}
    v7 = directives.get("paper_v7_authorization", {}) if isinstance(directives.get("paper_v7_authorization"), dict) else {}
    priorities = directives.get("current_priority_order", []) if isinstance(directives.get("current_priority_order"), list) else []
    lines = [
        "# Project context snapshot",
        "",
        f"- repository: `{snapshot.get('repository')}`",
        f"- head: `{snapshot.get('head')}`",
        f"- branch: `{snapshot.get('branch') or '<detached>'}`",
        f"- tracked files visible: **{snapshot.get('tracked_file_count')}**",
        f"- workflows visible: **{snapshot.get('workflow_count')}**",
        f"- schedulers registered: **{snapshot.get('scheduler_count')}**",
        f"- live champion: **V{champion.get('version')}** (`{champion.get('run_root')}`)",
        f"- canonical Grafana: `{grafana.get('canonical_operator_url')}`",
        f"- Grafana dashboard UID: `{grafana.get('dashboard_uid')}`",
        f"- runtime SSH alias: `{runtime.get('host_alias')}` -> `{runtime.get('user')}@{runtime.get('host')}:{runtime.get('port')}`",
        f"- operator directive epoch: `{directives.get('directive_epoch')}`",
        f"- operator directives: `{snapshot.get('operator_directives_path')}`",
        "",
        "## Current operator authorization",
        "",
        f"- V7 PAPER only: `{v7.get('paper_only')}`; authenticated execution: `{v7.get('authenticated_execution')}`",
        f"- fixed-dollar trade cap enabled: `{v7.get('fixed_dollar_trade_cap_enabled')}`",
        f"- trade / market / event / gross ceilings: `{v7.get('max_trade_fraction')}` / `{v7.get('max_market_fraction')}` / `{v7.get('max_event_fraction')}` / `{v7.get('max_gross_fraction')}`",
        f"- fractional Kelly ceiling: `{v7.get('fractional_kelly_ceiling')}`; drawdown kill: `{v7.get('max_drawdown')}`",
        "- older PR descriptions, comments and tests do not override this directive epoch",
        "",
        "## Current priority order",
        "",
    ]
    lines.extend(f"{index}. {item}" for index, item in enumerate(priorities, start=1))
    lines.extend([
        "",
        "## Repository surfaces",
        "",
        "| Surface | Tracked files |",
        "|---|---:|",
    ])
    for name, count in snapshot.get("top_level_counts", {}).items():
        lines.append(f"| `{name}` | {count} |")
    lines.extend([
        "",
        "Schedulers must use this snapshot and their entry in `operator_directives.scheduler_assignments` before narrowing to their bounded responsibility.",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize the complete Polymarket repository context for schedulers")
    parser.add_argument("--root", default=".")
    parser.add_argument("--manifest", default="config/project_context.json")
    parser.add_argument("--output-json", default="project-context.json")
    parser.add_argument("--output-markdown", default="project-context.md")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    manifest = Path(args.manifest)
    if not manifest.is_absolute():
        manifest = root / manifest
    snapshot = build_snapshot(root, manifest)
    Path(args.output_json).write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    Path(args.output_markdown).write_text(render_markdown(snapshot), encoding="utf-8")
    print(json.dumps({
        "head": snapshot["head"],
        "tracked_file_count": snapshot["tracked_file_count"],
        "workflow_count": snapshot["workflow_count"],
        "scheduler_count": snapshot["scheduler_count"],
        "champion_version": snapshot["live_champion"].get("version"),
        "grafana_uid": snapshot["grafana"].get("dashboard_uid"),
        "operator_directive_epoch": snapshot["operator_directives"].get("directive_epoch"),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
