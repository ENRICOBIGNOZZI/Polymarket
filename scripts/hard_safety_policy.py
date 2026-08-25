#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

PAPER_CONFIG_RE = re.compile(r"^config/paper_v\d+\.json$")
RUNTIME_HARD_SAFETY_SURFACE_PATTERNS = (
    re.compile(r"^scripts/paper_v\d+_(?:loop|once)(?:_v\d+)?\.sh$"),
    re.compile(r"^scripts/v\d+_materialize_configs(?:_v\d+)?\.py$"),
)

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
RUNTIME_PROTECTED_KEYS = (
    "max_drawdown",
    "max_market_fraction",
    "max_event_fraction",
    "max_gross_fraction",
    "global_max_drawdown",
    "global_max_gross_fraction",
    "paper_only",
)
_RUNTIME_KEY_ALT = "|".join(re.escape(key) for key in RUNTIME_PROTECTED_KEYS)
_RUNTIME_FLAG_ALT = "|".join(re.escape(key.replace("_", "-")) for key in RUNTIME_PROTECTED_KEYS)
_RUNTIME_ENV_ALT = "|".join(re.escape(key.upper()) for key in RUNTIME_PROTECTED_KEYS)
RUNTIME_HARD_SAFETY_WRITE_RE = re.compile(
    rf"(?:"
    rf"\[\s*['\"](?:{_RUNTIME_KEY_ALT})['\"]\s*\]\s*="
    rf"|['\"](?:{_RUNTIME_KEY_ALT})['\"]\s*:"
    rf"|--(?:{_RUNTIME_FLAG_ALT})\b"
    rf"|\b(?:{_RUNTIME_ENV_ALT})\s*="
    rf")"
)

# Current user-authorized V6 PAPER envelope. These are ceilings, not targets:
# model/research code may stay stricter, but governance must not reject V6 solely
# because the approved paper caps are above the historical 2.5% / 8% / 45% values.
V6_AUTHORIZED_CEILINGS = {
    "max_drawdown": 0.15,
    "max_market_fraction": 0.05,
    "max_event_fraction": 0.15,
    "max_gross_fraction": 0.70,
    "global_max_drawdown": 0.15,
    "global_max_gross_fraction": 0.70,
}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _compare_limit(
    errors: list[str],
    label: str,
    base_value: Any,
    current_value: Any,
    *,
    ceiling: float | None = None,
) -> None:
    base_number = _number(base_value)
    if base_number is None and ceiling is None:
        return
    current_number = _number(current_value)
    if current_number is None:
        errors.append(f"protected hard-safety limit removed or non-numeric: {label}")
        return
    allowed = float(ceiling) if ceiling is not None else float(base_number)
    if current_number > allowed + 1e-12:
        errors.append(
            f"protected hard-safety limit weakened: {label} allowed<={allowed:g}, got {current_number:g}"
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


def _is_v6(path: str) -> bool:
    return path == "config/paper_v6.json"


def _ceiling(path: str, key: str) -> float | None:
    if not _is_v6(path):
        return None
    return V6_AUTHORIZED_CEILINGS.get(key)


def compare_paper_config(base: dict[str, Any], current: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []

    for key in TOP_LEVEL_LIMITS:
        _compare_limit(
            errors,
            f"{path}:{key}",
            base.get(key),
            current.get(key),
            ceiling=_ceiling(path, key),
        )

    base_multi = base.get("multi_strategy") if isinstance(base.get("multi_strategy"), dict) else {}
    current_multi = current.get("multi_strategy") if isinstance(current.get("multi_strategy"), dict) else {}

    if (_is_v6(path) or base_multi.get("paper_only") is True) and current_multi.get("paper_only") is not True:
        errors.append(f"paper-only separation weakened: {path}:multi_strategy.paper_only must remain true")

    for key in MULTI_STRATEGY_LIMITS:
        _compare_limit(
            errors,
            f"{path}:multi_strategy.{key}",
            base_multi.get(key),
            current_multi.get(key),
            ceiling=_ceiling(path, key),
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
            if base_value is not None or _ceiling(path, key) is not None:
                _compare_limit(
                    errors,
                    f"{path}:strategy[{name}].{key}",
                    base_value,
                    current_value,
                    ceiling=_ceiling(path, key),
                )

    return errors


def is_runtime_hard_safety_surface(path: str) -> bool:
    return any(pattern.match(path) for pattern in RUNTIME_HARD_SAFETY_SURFACE_PATTERNS)


def compare_runtime_hard_safety(base_text: str, current_text: str, path: str) -> list[str]:
    """Reject newly introduced runtime writes to declarative hard-safety controls.

    Paper-only alpha/admission aggression may change model thresholds, universe,
    warm-up, cadence, sizing parameters and execution logic inside the approved
    envelope. Drawdown, concentration/gross ceilings and paper-only separation
    must remain explicit in versioned configuration rather than being silently
    weakened by new runtime/materialization overrides.
    """
    errors: list[str] = []
    for diff_line in difflib.ndiff(base_text.splitlines(), current_text.splitlines()):
        if not diff_line.startswith("+ "):
            continue
        line = diff_line[2:].strip()
        if not line or line.startswith("#"):
            continue
        if RUNTIME_HARD_SAFETY_WRITE_RE.search(line):
            errors.append(f"protected hard-safety runtime override added: {path}: {line}")
    return errors


def _git_show_text(base_ref: str, path: str) -> str | None:
    proc = subprocess.run(
        ["git", "show", f"{base_ref}:{path}"],
        text=True,
        capture_output=True,
        check=False,
    )
    return proc.stdout if proc.returncode == 0 else None


def _git_show_json(base_ref: str, path: str) -> dict[str, Any] | None:
    text = _git_show_text(base_ref, path)
    if text is None:
        return None
    data = json.loads(text)
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
        if PAPER_CONFIG_RE.match(path):
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
            continue

        if is_runtime_hard_safety_surface(path):
            current_path = root / path
            if not current_path.is_file():
                continue
            current_text = current_path.read_text(encoding="utf-8")
            base_text = _git_show_text(base_ref, path) or ""
            checked.append(f"{path} runtime-generated hard-safety controls")
            errors.extend(compare_runtime_hard_safety(base_text, current_text, path))

    return errors, checked


def render(errors: list[str], checked: list[str]) -> str:
    lines = [
        "# Hard-safety paper policy",
        "",
        f"- paper/runtime surfaces checked: {len(checked)}",
        f"- hard-safety violations: {len(errors)}",
        "- V6 authorized PAPER ceilings: drawdown 15%, market 5%, event 15%, gross 70%",
        "- paper alpha/admission aggression below those ceilings remains allowed; historical 2.5% / 8% / 45% values are not treated as immutable V6 caps",
        "- runtime rule: protected hard-safety controls may be inherited from versioned config, not newly hidden in runtime/materialization overrides",
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
