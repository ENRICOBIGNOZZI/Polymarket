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

PAPER_CONFIG_RE = re.compile(r"^config/paper_v\d+\.json$")
RUNTIME_HARD_SAFETY_SURFACE_PATTERNS = (
    re.compile(r"^scripts/paper_v\d+_(?:loop|once)(?:_v\d+)?\.sh$"),
    re.compile(r"^scripts/v\d+_materialize_configs(?:_v\d+)?\.py$"),
)

TOP_LEVEL_LIMITS = ("max_drawdown", "max_market_fraction", "max_event_fraction", "max_gross_fraction")
MULTI_STRATEGY_LIMITS = ("global_max_drawdown", "global_max_gross_fraction")
CHILD_LIMITS = TOP_LEVEL_LIMITS
RUNTIME_PROTECTED_KEYS = (
    "max_drawdown", "max_market_fraction", "max_event_fraction", "max_gross_fraction",
    "global_max_drawdown", "global_max_gross_fraction", "paper_only",
)
_RUNTIME_KEY_ALT = "|".join(re.escape(key) for key in RUNTIME_PROTECTED_KEYS)
_RUNTIME_FLAG_ALT = "|".join(re.escape(key.replace("_", "-")) for key in RUNTIME_PROTECTED_KEYS)
_RUNTIME_ENV_ALT = "|".join(re.escape(key.upper()) for key in RUNTIME_PROTECTED_KEYS)
RUNTIME_HARD_SAFETY_WRITE_RE = re.compile(
    rf"(?:\[\s*['\"](?:{_RUNTIME_KEY_ALT})['\"]\s*="
    rf"|['\"](?:{_RUNTIME_KEY_ALT})['\"]\s*:"
    rf"|--(?:{_RUNTIME_FLAG_ALT})\b"
    rf"|\b(?:{_RUNTIME_ENV_ALT})\s*=)"
)

# V6 remains frozen at the previously authorized PAPER envelope.
V6_AUTHORIZED_MARKET_LIMIT = 1000.0
V6_AUTHORIZED_CEILINGS = {
    "max_drawdown": 0.15,
    "max_market_fraction": 0.05,
    "max_event_fraction": 0.15,
    "max_gross_fraction": 0.70,
    "global_max_drawdown": 0.15,
    "global_max_gross_fraction": 0.70,
    "fractional_kelly": 0.25,
    "max_trade_usd": 125.0,
    "hard_arb_max_trade_usd": 125.0,
}

# V7 is a PAPER-only sandbox with the user-authorized 100% economic ceiling.
# A fixed-dollar cap is deliberately disabled: the effective hard ceiling is the
# available sleeve capital (100%). Authenticated execution must remain disabled.
V7_AUTHORIZED_MARKET_LIMIT = 1000.0
V7_AUTHORIZED_CEILINGS = {
    "max_drawdown": 0.15,
    "max_market_fraction": 1.0,
    "max_event_fraction": 1.0,
    "max_gross_fraction": 1.0,
    "global_max_drawdown": 0.15,
    "global_max_gross_fraction": 1.0,
    "fractional_kelly": 0.25,
    "max_trade_usd": math.inf,
    "hard_arb_max_trade_usd": math.inf,
    "max_trade_fraction": 1.0,
    "hard_arb_max_trade_fraction": 1.0,
}

V6_AUTHORIZED_FLOORS = {
    "min_liquidity": 2.0,
    "min_net_edge": 0.00005,
    "uncertainty_penalty": 0.0,
    "intent_min_edge": 0.00005,
    "hard_arb_min_net_edge": 0.00005,
}
V7_AUTHORIZED_FLOORS = dict(V6_AUTHORIZED_FLOORS)
V6_AUTHORIZED_CAPITAL_CEILINGS = {
    "micro_maker_capital_fraction": 0.22,
    "micro_taker_capital_fraction": 0.12,
    "relative_value_capital_fraction": 0.34,
    "hard_arb_capital_fraction": 0.22,
    "external_capital_fraction": 0.08,
    "reserve_fraction": 0.02,
}
V7_AUTHORIZED_CAPITAL_CEILINGS = dict(V6_AUTHORIZED_CAPITAL_CEILINGS)
V6_CAPITAL_FRACTIONS = tuple(V6_AUTHORIZED_CAPITAL_CEILINGS)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _is_v6(path: str) -> bool:
    return path == "config/paper_v6.json"


def _is_v7(path: str) -> bool:
    return path == "config/paper_v7.json"


def _is_authorized_paper(path: str) -> bool:
    return _is_v6(path) or _is_v7(path)


def _version_section(path: str) -> str | None:
    return "v6" if _is_v6(path) else "v7" if _is_v7(path) else None


def _ceilings(path: str) -> dict[str, float]:
    if _is_v6(path):
        return V6_AUTHORIZED_CEILINGS
    if _is_v7(path):
        return V7_AUTHORIZED_CEILINGS
    return {}


def _floors(path: str) -> dict[str, float]:
    if _is_v6(path):
        return V6_AUTHORIZED_FLOORS
    if _is_v7(path):
        return V7_AUTHORIZED_FLOORS
    return {}


def _capital_ceilings(path: str) -> dict[str, float]:
    if _is_v6(path):
        return V6_AUTHORIZED_CAPITAL_CEILINGS
    if _is_v7(path):
        return V7_AUTHORIZED_CAPITAL_CEILINGS
    return {}


def _market_limit(path: str) -> float | None:
    if _is_v6(path):
        return V6_AUTHORIZED_MARKET_LIMIT
    if _is_v7(path):
        return V7_AUTHORIZED_MARKET_LIMIT
    return None


def _ceiling(path: str, key: str) -> float | None:
    return _ceilings(path).get(key)


def _compare_limit(errors: list[str], label: str, base_value: Any, current_value: Any, *, ceiling: float | None = None) -> None:
    base_number = _number(base_value)
    if base_number is None and ceiling is None:
        return
    current_number = _number(current_value)
    if current_number is None:
        errors.append(f"protected hard-safety limit removed or non-numeric: {label}")
        return
    allowed = float(ceiling) if ceiling is not None else float(base_number)
    if current_number > allowed + 1e-12:
        errors.append(f"protected hard-safety limit weakened: {label} allowed<={allowed:g}, got {current_number:g}")


def _compare_floor(errors: list[str], label: str, current_value: Any, floor: float) -> None:
    current_number = _number(current_value)
    if current_number is None:
        errors.append(f"protected paper admission bound removed or non-numeric: {label}")
    elif current_number < floor - 1e-12:
        errors.append(f"authorized PAPER envelope violated: {label} required>={floor:g}, got {current_number:g}")


def _strategies(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    multi = config.get("multi_strategy")
    if not isinstance(multi, dict):
        return {}
    return {
        str(item["name"]): item
        for item in (multi.get("strategies") or [])
        if isinstance(item, dict) and item.get("name")
    }


def _fallback_limits(config: dict[str, Any]) -> dict[str, float | None]:
    multi = config.get("multi_strategy") if isinstance(config.get("multi_strategy"), dict) else {}
    gross = _number(multi.get("global_max_gross_fraction"))
    return {
        "max_drawdown": _number(config.get("max_drawdown")),
        "max_market_fraction": _number(config.get("max_market_fraction")),
        "max_event_fraction": _number(config.get("max_event_fraction")),
        "max_gross_fraction": gross if gross is not None else _number(config.get("max_gross_fraction")),
    }


def _validate_v7_paper_boundary(errors: list[str], current: dict[str, Any]) -> None:
    section = current.get("v7") if isinstance(current.get("v7"), dict) else {}
    if current.get("paper_only") is not True:
        errors.append("paper-only separation weakened: config/paper_v7.json:paper_only must remain true")
    if section.get("paper_only") is not True:
        errors.append("paper-only separation weakened: config/paper_v7.json:v7.paper_only must remain true")
    if section.get("authenticated_execution") is not False:
        errors.append("V7 PAPER authenticated execution must remain disabled: config/paper_v7.json:v7.authenticated_execution=false")
    if current.get("fixed_dollar_trade_cap_enabled") is not False:
        errors.append("V7 PAPER fixed-dollar trade cap must remain disabled; sizing ceiling is 100% of sleeve capital")
    if section.get("hard_arb_fixed_dollar_trade_cap_enabled") is not False:
        errors.append("V7 PAPER hard-arb fixed-dollar trade cap must remain disabled")
    _compare_limit(errors, "config/paper_v7.json:max_trade_fraction", None, current.get("max_trade_fraction"), ceiling=1.0)
    _compare_limit(errors, "config/paper_v7.json:v7.hard_arb_max_trade_fraction", None, section.get("hard_arb_max_trade_fraction"), ceiling=1.0)


def compare_paper_config(base: dict[str, Any], current: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    ceilings = _ceilings(path)
    floors = _floors(path)

    for key in TOP_LEVEL_LIMITS:
        _compare_limit(errors, f"{path}:{key}", base.get(key), current.get(key), ceiling=ceilings.get(key))

    base_multi = base.get("multi_strategy") if isinstance(base.get("multi_strategy"), dict) else {}
    current_multi = current.get("multi_strategy") if isinstance(current.get("multi_strategy"), dict) else {}
    if (_is_authorized_paper(path) or base_multi.get("paper_only") is True) and current_multi.get("paper_only") is not True:
        errors.append(f"paper-only separation weakened: {path}:multi_strategy.paper_only must remain true")
    for key in MULTI_STRATEGY_LIMITS:
        _compare_limit(errors, f"{path}:multi_strategy.{key}", base_multi.get(key), current_multi.get(key), ceiling=ceilings.get(key))

    base_strategies = _strategies(base)
    current_strategies = _strategies(current)
    base_fallback = _fallback_limits(base)
    current_fallback = _fallback_limits(current)
    for name, current_strategy in sorted(current_strategies.items()):
        current_overrides = current_strategy.get("overrides") if isinstance(current_strategy.get("overrides"), dict) else {}
        base_strategy = base_strategies.get(name, {})
        base_overrides = base_strategy.get("overrides") if isinstance(base_strategy.get("overrides"), dict) else {}
        for key in CHILD_LIMITS:
            base_value = base_overrides.get(key, base_fallback.get(key))
            current_value = current_overrides.get(key, current_fallback.get(key))
            if base_value is not None or ceilings.get(key) is not None:
                _compare_limit(errors, f"{path}:strategy[{name}].{key}", base_value, current_value, ceiling=ceilings.get(key))
        if _is_authorized_paper(path) and "min_net_edge" in current_overrides:
            _compare_floor(errors, f"{path}:strategy[{name}].min_net_edge", current_overrides.get("min_net_edge"), floors["min_net_edge"])
        for key in ("fractional_kelly", "max_trade_usd"):
            if _is_authorized_paper(path) and key in current_overrides:
                _compare_limit(errors, f"{path}:strategy[{name}].{key}", None, current_overrides.get(key), ceiling=ceilings.get(key))

    if _is_authorized_paper(path):
        _compare_limit(errors, f"{path}:market_limit", None, current.get("market_limit"), ceiling=_market_limit(path))
        for key in ("min_liquidity", "min_net_edge", "uncertainty_penalty"):
            _compare_floor(errors, f"{path}:{key}", current.get(key), floors[key])
        for key in ("fractional_kelly", "max_trade_usd"):
            _compare_limit(errors, f"{path}:{key}", None, current.get(key), ceiling=ceilings[key])

        section_name = _version_section(path)
        section = current.get(section_name) if section_name and isinstance(current.get(section_name), dict) else {}
        if not section_name or section.get("paper_only") is not True:
            errors.append(f"paper-only separation weakened: {path}:{section_name or 'version'}.paper_only must remain true")
        for key in ("intent_min_edge", "hard_arb_min_net_edge"):
            _compare_floor(errors, f"{path}:{section_name}.{key}", section.get(key), floors[key])
        _compare_limit(errors, f"{path}:{section_name}.hard_arb_max_trade_usd", None, section.get("hard_arb_max_trade_usd"), ceiling=ceilings["hard_arb_max_trade_usd"])

        capital_ceilings = _capital_ceilings(path)
        fractions: list[float] = []
        for key in V6_CAPITAL_FRACTIONS:
            value = _number(section.get(key))
            if value is None or value < -1e-12:
                errors.append(f"invalid {section_name.upper()} capital allocation: {path}:{section_name}.{key}")
            else:
                fractions.append(value)
                _compare_limit(errors, f"{path}:{section_name}.{key}", None, value, ceiling=capital_ceilings[key])
        if len(fractions) == len(V6_CAPITAL_FRACTIONS) and sum(fractions) > 1.0 + 1e-12:
            errors.append(f"{section_name.upper()} paper capital allocations exceed 100%: {path}:{section_name} total={sum(fractions):g}")

    if _is_v7(path):
        _validate_v7_paper_boundary(errors, current)
    return errors


def is_runtime_hard_safety_surface(path: str) -> bool:
    return any(pattern.match(path) for pattern in RUNTIME_HARD_SAFETY_SURFACE_PATTERNS)


def compare_runtime_hard_safety(base_text: str, current_text: str, path: str) -> list[str]:
    errors: list[str] = []
    for diff_line in difflib.ndiff(base_text.splitlines(), current_text.splitlines()):
        if not diff_line.startswith("+ "):
            continue
        line = diff_line[2:].strip()
        if line and not line.startswith("#") and RUNTIME_HARD_SAFETY_WRITE_RE.search(line):
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


def _baseline_config(base_ref: str, changed_path: str) -> tuple[str, dict[str, Any]] | None:
    same = _git_show_json(base_ref, changed_path)
    if same is not None:
        return changed_path, same
    manifest = _git_show_json(base_ref, "config/live_champion.json")
    if not manifest:
        return None
    incumbent_path = str(manifest.get("config") or "")
    incumbent = _git_show_json(base_ref, incumbent_path) if incumbent_path else None
    return (incumbent_path, incumbent) if incumbent_path and incumbent is not None else None


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
            baseline = _baseline_config(base_ref, path)
            if not isinstance(current, dict) or baseline is None:
                errors.append(f"cannot resolve incumbent hard-safety baseline for {path}")
                continue
            baseline_path, base = baseline
            checked.append(f"{path} against {baseline_path}")
            errors.extend(compare_paper_config(base, current, path))
        elif is_runtime_hard_safety_surface(path):
            current_path = root / path
            if current_path.is_file():
                checked.append(f"{path} runtime-generated hard-safety controls")
                errors.extend(compare_runtime_hard_safety(_git_show_text(base_ref, path) or "", current_path.read_text(encoding="utf-8"), path))
    return errors, checked


def render(errors: list[str], checked: list[str]) -> str:
    lines = [
        "# Hard-safety paper policy", "",
        f"- paper/runtime surfaces checked: {len(checked)}",
        f"- hard-safety violations: {len(errors)}",
        "- V6 PAPER envelope unchanged: universe <=1000, drawdown <=15%, market <=5%, event <=15%, gross <=70%, Kelly <=25%, max trade <=$125",
        "- V7 PAPER sandbox: universe <=1000, drawdown <=15%, market/event/gross/trade ceiling <=100% of sleeve capital, Kelly <=25%, no fixed-dollar trade cap",
        "- V7 authenticated execution is forbidden; v7.authenticated_execution must be false",
        "- V6/V7 sleeve allocation ceilings remain maker<=22%, taker<=12%, RV<=34%, hard<=22%, external<=8%, reserve<=2%",
        "- V6/V7 admission floors remain min liquidity >=$2, post-cost min edge >=0.5 bp, uncertainty penalty >=0",
        "- all changes still require normal research -> trusted governance -> integration provenance",
    ]
    lines.extend(f"- checked: `{item}`" for item in checked)
    if errors:
        lines += ["", "## Violations"] + [f"- {error}" for error in errors]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Prevent weakening versioned PAPER hard-safety contracts")
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
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
