#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import json
import math
import re
import subprocess
from pathlib import Path
from typing import Any

PAPER_CONFIG = "config/paper_v7.json"
RUNTIME_HARD_SAFETY_SURFACE_PATTERNS = (
    re.compile(r"^scripts/paper_v7_(?:loop|execution_loop)\.sh$"),
)
RUNTIME_PROTECTED_KEYS = (
    "max_drawdown",
    "max_trade_fraction",
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
    rf"\[\s*['\"](?:{_RUNTIME_KEY_ALT})['\"]\s*="
    rf"|['\"](?:{_RUNTIME_KEY_ALT})['\"]\s*:"
    rf"|--(?:{_RUNTIME_FLAG_ALT})\b"
    rf"|\b(?:{_RUNTIME_ENV_ALT})\s*="
    rf")"
)

V7_AUTHORIZED_MARKET_LIMIT = 1000.0
V7_AUTHORIZED_MAX_DRAWDOWN = 0.15
V7_AUTHORIZED_KELLY = 0.25
V7_AUTHORIZED_FLOORS = {
    "min_liquidity": 2.0,
    "min_net_edge": 0.00005,
    "uncertainty_penalty": 0.0,
    "intent_min_edge": 0.00005,
    "hard_arb_min_net_edge": 0.00005,
}
V7_HARD_PERCENTAGE = 1.0
V7_NONBINDING_DOLLAR_SENTINEL_MIN = 1e50
V7_SLEEVE_KEYS = (
    "micro_maker_capital_fraction",
    "micro_taker_capital_fraction",
    "relative_value_capital_fraction",
    "hard_arb_capital_fraction",
    "external_capital_fraction",
    "reserve_fraction",
)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = float(value)
    return out if math.isfinite(out) else None


def _ceiling(errors: list[str], label: str, value: Any, limit: float) -> None:
    current = _number(value)
    if current is None:
        errors.append(f"protected hard-safety limit removed or non-numeric: {label}")
    elif current > limit + 1e-12:
        errors.append(f"protected hard-safety limit weakened: {label} allowed<={limit:g}, got {current:g}")


def _floor(errors: list[str], label: str, value: Any, limit: float) -> None:
    current = _number(value)
    if current is None:
        errors.append(f"protected paper admission bound removed or non-numeric: {label}")
    elif current < limit - 1e-12:
        errors.append(f"authorized PAPER envelope violated: {label} required>={limit:g}, got {current:g}")


def _exact(errors: list[str], label: str, value: Any, expected: float) -> None:
    current = _number(value)
    if current is None or abs(current - expected) > 1e-12:
        errors.append(f"operator V7 directive violated: {label} required={expected:g}, got {value!r}")


def compare_paper_config(base: dict[str, Any], current: dict[str, Any], path: str) -> list[str]:
    del base
    if path != PAPER_CONFIG:
        return [f"retired/noncanonical paper configuration is not supported: {path}"]

    errors: list[str] = []
    if current.get("paper_only") is not True:
        errors.append(f"paper-only separation weakened: {path}:paper_only must remain true")
    if int(current.get("engine_version") or 0) != 7:
        errors.append(f"canonical PAPER engine must be V7: {path}:engine_version")

    _ceiling(errors, f"{path}:market_limit", current.get("market_limit"), V7_AUTHORIZED_MARKET_LIMIT)
    for key in ("min_liquidity", "min_net_edge", "uncertainty_penalty"):
        _floor(errors, f"{path}:{key}", current.get(key), V7_AUTHORIZED_FLOORS[key])
    _ceiling(errors, f"{path}:fractional_kelly", current.get("fractional_kelly"), V7_AUTHORIZED_KELLY)
    _ceiling(errors, f"{path}:max_drawdown", current.get("max_drawdown"), V7_AUTHORIZED_MAX_DRAWDOWN)

    if current.get("fixed_dollar_trade_cap_enabled") is not False:
        errors.append(f"operator V7 directive violated: {path}:fixed_dollar_trade_cap_enabled must be false")
    max_trade = _number(current.get("max_trade_usd"))
    if max_trade is None or max_trade < V7_NONBINDING_DOLLAR_SENTINEL_MIN:
        errors.append(f"operator V7 directive violated: {path}:max_trade_usd must be a nonbinding compatibility sentinel")

    for key in ("max_trade_fraction", "max_market_fraction", "max_event_fraction", "max_gross_fraction"):
        _exact(errors, f"{path}:{key}", current.get(key), V7_HARD_PERCENTAGE)

    multi = current.get("multi_strategy") if isinstance(current.get("multi_strategy"), dict) else {}
    if multi.get("paper_only") is not True:
        errors.append(f"paper-only separation weakened: {path}:multi_strategy.paper_only must remain true")
    _ceiling(errors, f"{path}:multi_strategy.global_max_drawdown", multi.get("global_max_drawdown"), V7_AUTHORIZED_MAX_DRAWDOWN)
    _exact(errors, f"{path}:multi_strategy.global_max_gross_fraction", multi.get("global_max_gross_fraction"), V7_HARD_PERCENTAGE)

    v7 = current.get("v7") if isinstance(current.get("v7"), dict) else {}
    if v7.get("paper_only") is not True:
        errors.append(f"paper-only separation weakened: {path}:v7.paper_only must remain true")
    if v7.get("authenticated_execution") is not False:
        errors.append(f"authenticated execution separation weakened: {path}:v7.authenticated_execution must remain false")
    for key in ("authoritative_fee_required", "shared_execution_ledger_required", "joint_fill_state_required_for_multileg"):
        if v7.get(key) is not True:
            errors.append(f"V7 execution evidence requirement weakened: {path}:v7.{key} must remain true")
    for key in ("intent_min_edge", "hard_arb_min_net_edge"):
        _floor(errors, f"{path}:v7.{key}", v7.get(key), V7_AUTHORIZED_FLOORS[key])
    if v7.get("hard_arb_fixed_dollar_trade_cap_enabled") is not False:
        errors.append(f"operator V7 directive violated: {path}:v7.hard_arb_fixed_dollar_trade_cap_enabled must be false")
    _exact(errors, f"{path}:v7.hard_arb_max_trade_fraction", v7.get("hard_arb_max_trade_fraction"), V7_HARD_PERCENTAGE)
    hard_trade = _number(v7.get("hard_arb_max_trade_usd"))
    if hard_trade is None or hard_trade < V7_NONBINDING_DOLLAR_SENTINEL_MIN:
        errors.append(f"operator V7 directive violated: {path}:v7.hard_arb_max_trade_usd must be a nonbinding compatibility sentinel")

    fractions: list[float] = []
    for key in V7_SLEEVE_KEYS:
        value = _number(v7.get(key))
        if value is None or value < -1e-12:
            errors.append(f"invalid V7 capital allocation: {path}:v7.{key}")
        else:
            fractions.append(value)
    if len(fractions) == len(V7_SLEEVE_KEYS) and abs(sum(fractions) - 1.0) > 1e-9:
        errors.append(f"V7 paper capital allocations must sum to 100%: {path}:v7 total={sum(fractions):g}")

    if "v6" in current:
        errors.append(f"retired compatibility namespace present: {path}:v6")
    return errors


def is_runtime_hard_safety_surface(path: str) -> bool:
    return any(pattern.match(path) for pattern in RUNTIME_HARD_SAFETY_SURFACE_PATTERNS)


def compare_runtime_hard_safety(base_text: str, current_text: str, path: str) -> list[str]:
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
    proc = subprocess.run(["git", "show", f"{base_ref}:{path}"], text=True, capture_output=True, check=False)
    return proc.stdout if proc.returncode == 0 else None


def _git_show_json(base_ref: str, path: str) -> dict[str, Any] | None:
    text = _git_show_text(base_ref, path)
    if text is None:
        return None
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def evaluate(base_ref: str, changed_files: set[str], root: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    checked: list[str] = []
    for path in sorted(changed_files):
        if re.fullmatch(r"config/paper_v\d+\.json", path):
            if path != PAPER_CONFIG:
                if (root / path).exists():
                    errors.append(f"retired paper configuration reintroduced: {path}")
                continue
            current_path = root / path
            if not current_path.is_file():
                errors.append(f"protected paper configuration removed: {path}")
                continue
            current = json.loads(current_path.read_text(encoding="utf-8"))
            base = _git_show_json(base_ref, path) or {}
            checked.append(path)
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
        "# Hard-safety paper policy", "",
        f"- V7 paper/runtime surfaces checked: {len(checked)}",
        f"- hard-safety violations: {len(errors)}",
        "- canonical runtime generation: V7 only",
        "- authorization: PAPER-only, authenticated execution disabled, no binding fixed-dollar cap, trade/market/event/gross hard percentage ceilings =100%, Kelly <=25%, drawdown <=15%",
        "- admission floors: min liquidity >=$2, post-cost min edge >=0.5 bp, uncertainty penalty >=0; authoritative fees, shared execution ledger and joint multi-leg fill-state evidence remain mandatory",
        "- 100% values are ceilings, not trade targets; executable depth/costs, available capital, state integrity and the drawdown kill remain binding",
    ]
    for item in checked:
        lines.append(f"- checked: `{item}`")
    if errors:
        lines.extend(["", "## Violations"])
        lines.extend(f"- {error}" for error in errors)
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Prevent weakening or contradicting canonical V7 PAPER hard safety")
    parser.add_argument("--base-ref", default="origin/main")
    parser.add_argument("--changed-files", required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    changed = {line.strip() for line in Path(args.changed_files).read_text(encoding="utf-8").splitlines() if line.strip()}
    try:
        errors, checked = evaluate(args.base_ref, changed, root)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors, checked = [str(exc)], []
    report = render(errors, checked)
    Path(args.output).write_text(report, encoding="utf-8")
    print(report, end="")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
