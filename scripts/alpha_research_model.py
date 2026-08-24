"""Configuration, candidates and fixed scanner commands for alpha research."""
from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = "polymarket_alpha_research_v1"
FAMILIES = {"B1", "B2"}

B1_DEFAULTS: dict[str, float | int] = {
    "markets": 600,
    "history_universe": 160,
    "lookback_hours": 336,
    "fidelity_minutes": 30,
    "min_z": 1.50,
    "max_half_life_hours": 168.0,
    "min_t_reversion": 1.75,
    "top": 80,
}
B2_DEFAULTS: dict[str, float | int] = {
    "markets": 600,
    "universe": 120,
    "lookback_hours": 336,
    "fidelity_minutes": 30,
    "factors": 3,
    "max_hedges": 4,
    "min_z": 1.50,
    "max_half_life_hours": 168.0,
    "min_t_reversion": 1.75,
    "max_factor_hedge_error": 0.20,
    "top": 80,
}

INT_KEYS = {
    "B1": {"markets", "history_universe", "lookback_hours", "fidelity_minutes", "top"},
    "B2": {"markets", "universe", "lookback_hours", "fidelity_minutes", "factors", "max_hedges", "top"},
}
FLOAT_KEYS = {
    "B1": {"min_z", "max_half_life_hours", "min_t_reversion"},
    "B2": {"min_z", "max_half_life_hours", "min_t_reversion", "max_factor_hedge_error"},
}


class ConfigError(ValueError):
    """Invalid alpha-research configuration."""


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    family: str
    params: dict[str, float | int]
    champion: bool = False
    hypothesis: str = ""
    oos_report: str = ""
    execution_min_edge: float = 0.001


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, indent=2, sort_keys=True) + "\n")


def finite_number(value: Any, name: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be numeric") from exc
    if not math.isfinite(out):
        raise ConfigError(f"{name} must be finite")
    return out


def normalize_params(family: str, raw: dict[str, Any]) -> dict[str, float | int]:
    if family not in FAMILIES:
        raise ConfigError(f"unsupported family: {family}")
    if not isinstance(raw, dict):
        raise ConfigError(f"params for {family} must be an object")
    defaults = B1_DEFAULTS if family == "B1" else B2_DEFAULTS
    allowed = set(defaults)
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"unknown {family} parameters: {', '.join(unknown)}")
    merged: dict[str, float | int] = dict(defaults)
    for key, value in raw.items():
        if key in INT_KEYS[family]:
            number = finite_number(value, f"{family}.{key}")
            if number != int(number) or number <= 0:
                raise ConfigError(f"{family}.{key} must be a positive integer")
            merged[key] = int(number)
        elif key in FLOAT_KEYS[family]:
            number = finite_number(value, f"{family}.{key}")
            if number <= 0:
                raise ConfigError(f"{family}.{key} must be positive")
            merged[key] = number
    if float(merged["min_z"]) < 0.50:
        raise ConfigError(f"{family}.min_z below the research floor 0.50")
    if float(merged["max_half_life_hours"]) > 24.0 * 30.0:
        raise ConfigError(f"{family}.max_half_life_hours exceeds 30 days")
    if family == "B2":
        if int(merged["max_hedges"]) < 1:
            raise ConfigError("B2.max_hedges must be >= 1")
        if int(merged["factors"]) < 1:
            raise ConfigError("B2.factors must be >= 1")
        if float(merged["max_factor_hedge_error"]) > 0.50:
            raise ConfigError("B2.max_factor_hedge_error exceeds the research ceiling 0.50")
    return merged


def safe_id(raw: Any, field: str) -> str:
    value = str(raw or "").strip()
    if not value or len(value) > 80:
        raise ConfigError(f"{field} must contain 1-80 characters")
    if any(not (c.isalnum() or c in "-_") for c in value):
        raise ConfigError(f"{field} may contain only letters, digits, '-' and '_'")
    return value


def load_config(path: Path) -> dict[str, Any]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read config: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON config: {exc}") from exc
    if not isinstance(obj, dict):
        raise ConfigError("config root must be an object")
    if obj.get("schema") != SCHEMA:
        raise ConfigError(f"schema must be {SCHEMA}")

    cadence = int(finite_number(obj.get("cadence_seconds", 21600), "cadence_seconds"))
    max_challengers = int(finite_number(obj.get("max_challengers_per_cycle", 2), "max_challengers_per_cycle"))
    if cadence < 3600:
        raise ConfigError("cadence_seconds must be at least one hour")
    if not 1 <= max_challengers <= 8:
        raise ConfigError("max_challengers_per_cycle must be in [1, 8]")

    champions_raw = obj.get("champions")
    if not isinstance(champions_raw, dict) or set(champions_raw) != FAMILIES:
        raise ConfigError("champions must define exactly B1 and B2")
    champions: dict[str, Candidate] = {}
    for family in sorted(FAMILIES):
        raw = champions_raw[family]
        if not isinstance(raw, dict):
            raise ConfigError(f"champion {family} must be an object")
        champion_edge = finite_number(raw.get("execution_min_edge", 0.001), f"champions.{family}.execution_min_edge")
        if champion_edge < 0.001:
            raise ConfigError(f"champions.{family}.execution_min_edge is below the production floor 0.001")
        champions[family] = Candidate(
            candidate_id=safe_id(raw.get("id", f"{family.lower()}_champion"), f"champions.{family}.id"),
            family=family,
            params=normalize_params(family, raw.get("params", {})),
            champion=True,
            hypothesis=str(raw.get("hypothesis", "current production champion")),
            oos_report=str(raw.get("oos_report", f"{{run_root}}/alpha_research/oos/{family.lower()}_champion.json")),
            execution_min_edge=champion_edge,
        )

    challengers_raw = obj.get("challengers", [])
    if not isinstance(challengers_raw, list) or not challengers_raw:
        raise ConfigError("challengers must be a non-empty array")
    challengers: list[Candidate] = []
    seen = {c.candidate_id for c in champions.values()}
    for idx, raw in enumerate(challengers_raw):
        if not isinstance(raw, dict):
            raise ConfigError(f"challengers[{idx}] must be an object")
        family = str(raw.get("family", "")).upper()
        if family not in FAMILIES:
            raise ConfigError(f"challengers[{idx}].family must be B1 or B2")
        candidate_id = safe_id(raw.get("id"), f"challengers[{idx}].id")
        if candidate_id in seen:
            raise ConfigError(f"duplicate candidate id: {candidate_id}")
        seen.add(candidate_id)
        execution_min_edge = finite_number(
            raw.get("execution_min_edge", champions[family].execution_min_edge),
            f"challengers[{idx}].execution_min_edge",
        )
        if execution_min_edge < 0.001:
            raise ConfigError(f"challengers[{idx}].execution_min_edge is below the research floor 0.001")
        merged = dict(champions[family].params)
        overrides = raw.get("params", {})
        if not isinstance(overrides, dict):
            raise ConfigError(f"challengers[{idx}].params must be an object")
        merged.update(overrides)
        challengers.append(Candidate(
            candidate_id=candidate_id,
            family=family,
            params=normalize_params(family, merged),
            champion=False,
            hypothesis=str(raw.get("hypothesis", "")).strip(),
            oos_report=str(raw.get("oos_report", f"{{run_root}}/alpha_research/oos/{candidate_id}.json")),
            execution_min_edge=execution_min_edge,
        ))

    obj["cadence_seconds"] = cadence
    obj["max_challengers_per_cycle"] = max_challengers
    obj["_champions"] = champions
    obj["_challengers"] = challengers
    return obj


def select_challengers(config: dict[str, Any], now: int) -> tuple[int, list[Candidate]]:
    challengers: list[Candidate] = config["_challengers"]
    cadence = int(config["cadence_seconds"])
    cycle_index = max(0, now // cadence)
    count = min(int(config["max_challengers_per_cycle"]), len(challengers))
    start = (cycle_index * count) % len(challengers)
    selected = [challengers[(start + i) % len(challengers)] for i in range(count)]
    return cycle_index, selected


def scanner_command(candidate: Candidate, build_dir: Path, paper_config: Path, output_csv: Path) -> list[str]:
    p = candidate.params
    if candidate.family == "B1":
        return [
            str(build_dir / "polymarket_stat_arb"),
            "--config", str(paper_config),
            "--markets", str(p["markets"]),
            "--history-universe", str(p["history_universe"]),
            "--lookback-hours", str(p["lookback_hours"]),
            "--fidelity-minutes", str(p["fidelity_minutes"]),
            "--min-z", str(p["min_z"]),
            "--max-half-life-hours", str(p["max_half_life_hours"]),
            "--min-t-reversion", str(p["min_t_reversion"]),
            "--top", str(p["top"]),
            "--csv", str(output_csv),
        ]
    return [
        str(build_dir / "polymarket_pca_stat_arb"),
        "--config", str(paper_config),
        "--markets", str(p["markets"]),
        "--universe", str(p["universe"]),
        "--lookback-hours", str(p["lookback_hours"]),
        "--fidelity-minutes", str(p["fidelity_minutes"]),
        "--factors", str(p["factors"]),
        "--max-hedges", str(p["max_hedges"]),
        "--min-z", str(p["min_z"]),
        "--max-half-life-hours", str(p["max_half_life_hours"]),
        "--min-t-reversion", str(p["min_t_reversion"]),
        "--max-factor-hedge-error", str(p["max_factor_hedge_error"]),
        "--top", str(p["top"]),
        "--csv", str(output_csv),
    ]

