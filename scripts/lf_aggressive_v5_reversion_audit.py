#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass

MASK64 = (1 << 64) - 1


@dataclass(frozen=True)
class GateConfig:
    name: str
    min_series: int
    min_transitions: int
    min_half: int
    phi_floor: float
    min_t: float
    min_stability: float
    max_half_life_hours: float


INCUMBENT = GateConfig(
    name="incumbent",
    min_series=24,
    min_transitions=20,
    min_half=24,
    phi_floor=0.02,
    min_t=1.75,
    min_stability=0.45,
    max_half_life_hours=168.0,
)

# Effective B2 mean-reversion admission in the proposed persistent paper loop:
# pca_stat_arb receives --min-t-reversion 0.60 and --max-half-life-hours 336,
# while the source implementation itself uses the relaxed 16/12/12 sample
# requirements, phi floor 0.05 and stability cutoff 0.25.
AGGRESSIVE_V5 = GateConfig(
    name="aggressive_v5_pr154_paper_loop",
    min_series=16,
    min_transitions=12,
    min_half=12,
    phi_floor=0.05,
    min_t=0.60,
    min_stability=0.25,
    max_half_life_hours=336.0,
)


class StableNormal:
    """Small deterministic RNG so the regression fixture is version-stable."""

    def __init__(self, seed: int):
        self.state = seed & MASK64 or 0x9E3779B97F4A7C15
        self.spare: float | None = None

    def _u64(self) -> int:
        x = self.state
        x ^= x >> 12
        x ^= (x << 25) & MASK64
        x ^= x >> 27
        self.state = x & MASK64
        return (self.state * 2685821657736338717) & MASK64

    def uniform(self) -> float:
        return ((self._u64() >> 11) + 0.5) / float(1 << 53)

    def normal(self) -> float:
        if self.spare is not None:
            value = self.spare
            self.spare = None
            return value
        u1 = max(1e-16, self.uniform())
        u2 = self.uniform()
        radius = math.sqrt(-2.0 * math.log(u1))
        angle = 2.0 * math.pi * u2
        self.spare = radius * math.sin(angle)
        return radius * math.cos(angle)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def fit_residual(values: list[float], config: GateConfig) -> dict[str, float | bool] | None:
    if len(values) < config.min_series:
        return None
    mean = _mean(values)
    variance = sum((value - mean) ** 2 for value in values) / max(1, len(values) - 1)
    if math.sqrt(max(0.0, variance)) < 1e-5:
        return None

    lag = values[:-1]
    delta = [values[index] - values[index - 1] for index in range(1, len(values))]
    if len(lag) < config.min_transitions:
        return None
    lag_mean = _mean(lag)
    delta_mean = _mean(delta)
    sxx = sum((value - lag_mean) ** 2 for value in lag)
    if sxx < 1e-10:
        return None
    sxy = sum((lag[index] - lag_mean) * (delta[index] - delta_mean) for index in range(len(lag)))
    gamma = sxy / sxx
    intercept = delta_mean - gamma * lag_mean
    rss = sum(
        (delta[index] - (intercept + gamma * lag[index])) ** 2
        for index in range(len(lag))
    )
    sigma2 = rss / max(1, len(lag) - 2)
    se = math.sqrt(max(0.0, sigma2) / sxx)
    t_reversion = gamma / se if se > 1e-12 else 0.0
    phi = 1.0 + gamma
    ok = gamma < 0.0 and config.phi_floor < phi < 0.999 and math.isfinite(t_reversion)
    return {"phi": phi, "t_reversion": t_reversion, "ok": ok}


def passes_gate(values: list[float], config: GateConfig, bucket_hours: float = 0.5) -> bool:
    full = fit_residual(values, config)
    if not full or not full["ok"] or float(full["t_reversion"]) > -config.min_t:
        return False

    cut = len(values) // 2
    if cut < config.min_half or len(values) - cut < config.min_half:
        return False
    first = fit_residual(values[:cut], config)
    second = fit_residual(values[cut:], config)
    if not first or not second or not first["ok"] or not second["ok"]:
        return False

    stability = math.exp(-4.0 * abs(float(first["phi"]) - float(second["phi"])))
    if stability < config.min_stability:
        return False

    phi = float(full["phi"])
    half_life = -math.log(2.0) / math.log(phi) * bucket_hours
    return math.isfinite(half_life) and 0.0 < half_life <= config.max_half_life_hours


def unit_root_paths(count: int, length: int, innovation_rho: float, seed: int) -> list[list[float]]:
    rng = StableNormal(seed)
    output: list[list[float]] = []
    for _ in range(count):
        values = [0.0]
        previous_innovation = 0.0
        for _ in range(1, length):
            innovation = innovation_rho * previous_innovation + rng.normal()
            values.append(values[-1] + innovation)
            previous_innovation = innovation
        output.append(values)
    return output


def stationary_paths(count: int, length: int, phi: float, seed: int) -> list[list[float]]:
    rng = StableNormal(seed)
    output: list[list[float]] = []
    for _ in range(count):
        values = [rng.normal()]
        for _ in range(1, length):
            values.append(phi * values[-1] + rng.normal())
        output.append(values)
    return output


def summarize(count: int = 200) -> dict[str, object]:
    experiments: list[dict[str, object]] = []
    for length in (48, 96, 672):
        for innovation_rho in (0.0, 0.65):
            seed = 0x20260825 + length + int(innovation_rho * 1000)
            paths = unit_root_paths(count, length, innovation_rho, seed)
            aggressive_pass = sum(passes_gate(path, AGGRESSIVE_V5) for path in paths)
            incumbent_pass = sum(passes_gate(path, INCUMBENT) for path in paths)
            experiments.append({
                "process": "unit_root",
                "length": length,
                "innovation_rho": innovation_rho,
                "paths": count,
                "incumbent_pass": incumbent_pass,
                "incumbent_rate": incumbent_pass / count,
                "aggressive_pass": aggressive_pass,
                "aggressive_rate": aggressive_pass / count,
            })

        stationary = stationary_paths(count, length, 0.90, 0xABC000 + length)
        experiments.append({
            "process": "stationary_ar1",
            "phi": 0.90,
            "length": length,
            "paths": count,
            "incumbent_pass": sum(passes_gate(path, INCUMBENT) for path in stationary),
            "aggressive_pass": sum(passes_gate(path, AGGRESSIVE_V5) for path in stationary),
        })

    return {
        "schema": "polymarket_lf_aggressive_v5_reversion_audit_v1",
        "source_research_pr": 154,
        "source_research_head": "8b39ceb5182432def738ffdee2d2c7ba8c5567f1",
        "integration_pr": 156,
        "incumbent": asdict(INCUMBENT),
        "aggressive_v5": asdict(AGGRESSIVE_V5),
        "experiments": experiments,
        "interpretation": (
            "The relaxed V5 paper-loop gate materially increases the probability that deterministic unit-root "
            "paths pass the mean-reversion admission rule. These controlled fixtures diagnose statistical size, "
            "not live Polymarket false-discovery rates or trading PnL."
        ),
        "decision": "MORE_EVIDENCE_REQUIRED",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--paths", type=int, default=200)
    args = parser.parse_args()
    print(json.dumps(summarize(max(1, args.paths)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
