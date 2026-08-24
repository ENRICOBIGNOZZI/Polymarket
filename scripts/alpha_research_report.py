"""Process execution and report rendering for alpha research."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

def run_scanner(command: list[str], stdout_path: Path, stderr_path: Path, timeout_seconds: int) -> dict[str, Any]:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with stdout_path.open("w", encoding="utf-8") as out, stderr_path.open("w", encoding="utf-8") as err:
        try:
            completed = subprocess.run(command, stdout=out, stderr=err, check=False, timeout=timeout_seconds)
            return {
                "returncode": completed.returncode,
                "timed_out": False,
                "duration_seconds": time.time() - started,
            }
        except subprocess.TimeoutExpired:
            return {
                "returncode": 124,
                "timed_out": True,
                "duration_seconds": time.time() - started,
            }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Polymarket alpha research cycle",
        "",
        f"- Cycle: `{report['cycle_index']}`",
        f"- Started: `{report['started_ts']}`",
        f"- Production modified: **no**",
        f"- Promotion-ready candidates: **{report['promotion_ready_count']}**",
        "",
        "| Candidate | Family | Stage | Maker+ | Best edge | Screen Δ | OOS decision |",
        "|---|---:|---|---:|---:|---:|---|",
    ]
    for item in report["candidates"]:
        metrics = item.get("metrics", {})
        screen = item.get("screen", {})
        lines.append(
            "| {id} | {family} | {stage} | {pos} | {edge:.6f} | {delta:.6f} | {oos} |".format(
                id=item["id"],
                family=item["family"],
                stage=item["stage"],
                pos=int(metrics.get("maker_positive", 0) or 0),
                edge=float(metrics.get("best_maker_edge", 0.0) or 0.0),
                delta=float(screen.get("absolute_improvement", 0.0) or 0.0),
                oos=", ".join(item.get("promotion_failures", [])) or "pass",
            )
        )
    lines.extend([
        "",
        "A screen pass only moves a challenger into shadow/OOS research. Production parameters remain unchanged until a separate PR is reviewed and the live-paper deployment gate passes.",
        "",
    ])
    return "\n".join(lines)


def append_history(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n")


