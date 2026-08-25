#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

PAPER_CONFIG_RE = re.compile(r"^config/paper_v\d+\.json$")

TOP_LEVEL_LIMITS = (
    "max_drawdown",
    "max_market_fraction",
    "max_event_fraction",
    "max_gross_fraction",
)
MULTI_STRATEGY_LIMITS = (
    "global_max_drawdown",
    "global_max_gross_fraction",
)
CHILD_LIMITS = (
    "max_drawdown",
    "max_market_fraction",
    "max_event_fraction",
    "max_gross_fraction",
)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _compare_limit(errors: list[str], label: str, base_value: Any, current_value: Any) -> None:
    base_number = _number(base_value)
    if base_number is None:
        return
    current_number = _number(current_value)
    if current_number is None:
        errors.append(f"protected hard-safety limit removed or non-numeric: {label}")
        return
    if current_number > base_number + 1e-12:
        errors.append(
            f"protected hard-safety limit weakened: {label} {base_number:g} -> {current_number:g}"
        )


def _strategies(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    multi = config.get("multi_strategy")
    if not isinstance(multi, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in multi.get("strategies") or []:
        if isinstance(item, dict) and item.get("name"):
            result[str(item["name"])] = item
    return result


def _fallback_limits(config: dict[str, Any]) -> dict[str, float | None]:
    multi = config.get("multi_strategy") if isinstance(config.get("multi_strategy"), dict) else {}
    return {
        "max_drawdown": _number(config.get("max_drawdown")),
        "max_market_fraction": _number(config.get("max_market_fraction")),
        "max_event_fraction": _number(config.get("max_event_fraction")),
        "max_gross_fraction": _number(multi.get("global_max_gross_fraction"))
        or _number(config.get("max_gross_fraction")),
    }


def compare_paper_config(base: dict[str, Any], current: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []

    for key in TOP_LEVEL_LIMITS:
        _compare_limit(errors, f"{path}:{key}", base.get(key), current.get(key))

    base_multi = base.get("multi_strategy") if isinstance(base.get("multi_strategy"), dict) else {}
    current_multi = current.get("multi_strategy") if isinstance(current.get("multi_strategy"), dict) else {}

    if base_multi.get("paper_only") is True and current_multi.get("paper_only") is not True:
        errors.append(f"paper-only separation weakened: {path}:multi_strategy.paper_only must remain true")

    for key in MULTI_STRATEGY_LIMITS:
        _compare_limit(
            errors,
            f"{path}:multi_strategy.{key}",
            base_multi.get(key),
            current_multi.get(key),
        )

    base_strategies = _strategies(base)
    current_strategies = _strategies(current)
    base_fallback = _fallback_limits(base)
    current_fallback = _fallback_limits(current)

    for name, current_strategy in sorted(current_strategies.items()):
        current_overrides = (
            current_strategy.get("overrides")
            if isinstance(current_strategy.get("overrides"), dict)
            else {}
        )
        base_strategy = base_strategies.get(name, {})
        base_overrides = (
            base_strategy.get("overrides")
            if isinstance(base_strategy.get("overrides"), dict)
            else {}
        )
        for key in CHILD_LIMITS:
            base_value = base_overrides.get(key, base_fallback.get(key))
            current_value = current_overrides.get(key, current_fallback.get(key))
            if base_value is not None:
                _compare_limit(
                    errors,
                    f"{path}:strategy[{name}].{key}",
                    base_value,
                    current_value,
                )

    return errors


def _git_show_json(base_ref: str, path: str) -> dict[str, Any] | None:
    proc = subprocess.run(
        ["git", "show", f"{base_ref}:{path}"],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    data = json.loads(proc.stdout)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _baseline_config(base_ref: str, changed_path: str) -> tuple[str, dict[str, Any]] | None:
    same_path = _git_show_json(base_ref, changed_path)
    if same_path is not None:
        return changed_path, same_path
    manifest = _git_show_json(base_ref, "config/live_champion.json")
    if not manifest:
        return None
    incumbent_path = str(manifest.get("config") or "")
    if not incumbent_path:
        return None
    incumbent = _git_show_json(base_ref, incumbent_path)
    if incumbent is None:
        return None
    return incumbent_path, incumbent


def evaluate(base_ref: str, changed_files: set[str], root: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    checked: list[str] = []
    for path in sorted(changed_files):
        if not PAPER_CONFIG_RE.match(path):
            continue
        current_path = root / path
        if not current_path.is_file():
            errors.append(f"protected paper configuration removed: {path}")
            continue
        current = json.loads(current_path.read_text(encoding="utf-8"))
        if not isinstance(current, dict):
            errors.append(f"{path} must contain a JSON object")
            continue
        baseline = _baseline_config(base_ref, path)
        if baseline is None:
            errors.append(f"cannot resolve incumbent hard-safety baseline for {path}")
            continue
        baseline_path, base = baseline
        checked.append(f"{path} against {baseline_path}")
        errors.extend(compare_paper_config(base, current, path))
    return errors, checked


def render(errors: list[str], checked: list[str]) -> str:
    lines = [
        "# Hard-safety paper policy",
        "",
        f"- paper configs checked: {len(checked)}",
        f"- hard-safety violations: {len(errors)}",
        "- paper alpha/evaluation aggression: allowed when hard-safety limits remain at least as strict as the incumbent",
    ]
    for item in checked:
        lines.append(f"- checked: `{item}`")
    if errors:
        lines.extend(["", "## Violations"])
        lines.extend(f"- {error}" for error in errors)
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Prevent weakening hard safety in paper model/evaluation changes")
    parser.add_argument("--base-ref", default="origin/main")
    parser.add_argument("--changed-files", required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    root = Path(args.root).resolve()
    changed = {
        line.strip()
        for line in Path(args.changed_files).read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    try:
        errors, checked = evaluate(args.base_ref, changed, root)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors, checked = [str(exc)], []
    report = render(errors, checked)
    Path(args.output).write_text(report, encoding="utf-8")
    print(report, end="")
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
