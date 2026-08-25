#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

MASK64 = (1 << 64) - 1


class StableNormal:
    """Deterministic Gaussian generator for version-stable regression fixtures."""

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


def _sample_sd(values: list[float], mean: float) -> float:
    if len(values) < 2:
        return 0.0
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


def incumbent_gate(
    residual_returns: list[float],
    current_residual_return: float,
    min_residual_z: float = 0.60,
) -> dict[str, float | int | bool]:
    """Reproduce the V5 Engine::pca_adjustments rolling-state reversion gate.

    This isolates the idiosyncratic residual regression after PCA. The supplied
    residual returns are already standardized, matching the scale on which the
    engine constructs `resid[t][j]`.
    """

    T = len(residual_returns)
    H = min(12, max(4, T // 4))
    states: list[float] = []
    next_resid: list[float] = []
    for t in range(H - 1, T - 1):
        states.append(sum(residual_returns[t + 1 - H : t + 1]))
        next_resid.append(residual_returns[t + 1])
    if len(states) < 8:
        return {"admitted": False, "T": T, "H": H, "rows": len(states)}

    state_mu = _mean(states)
    state_sd = max(0.20, _sample_sd(states, state_mu))
    state_now = current_residual_return + sum(residual_returns[T - (H - 1) : T])
    x_now = state_now - state_mu
    z = x_now / state_sd

    y_mu = _mean(next_resid)
    sxx = 0.0
    sxy = 0.0
    for state, target in zip(states, next_resid):
        x = state - state_mu
        y = target - y_mu
        sxx += x * x
        sxy += x * y
    if sxx <= 1e-8:
        return {"admitted": False, "T": T, "H": H, "rows": len(states), "z": z}

    ridge = 0.05 * len(states) * state_sd * state_sd
    beta = sxy / (sxx + ridge)
    rss = sum(
        (target - (y_mu + beta * (state - state_mu))) ** 2
        for state, target in zip(states, next_resid)
    )
    sigma2 = rss / max(1, len(states) - 2)
    se_beta = math.sqrt(max(0.0, sigma2) / max(1e-8, sxx + ridge))
    t_reversion = -beta / se_beta if se_beta > 1e-8 else 0.0
    forecast_std = y_mu + beta * x_now

    admitted = (
        abs(z) >= min_residual_z
        and beta < -1e-4
        and t_reversion >= 0.75
        and forecast_std * x_now < 0.0
    )
    return {
        "admitted": admitted,
        "T": T,
        "H": H,
        "rows": len(states),
        "z": z,
        "beta": beta,
        "t_reversion": t_reversion,
        "forecast_std": forecast_std,
    }


def iid_null_experiment(T: int, paths: int = 400, seed: int = 0x20260825) -> dict[str, float | int]:
    rng = StableNormal(seed + T)
    admitted = 0
    beta_sum = 0.0
    t_sum = 0.0
    z_gate = 0
    H = min(12, max(4, T // 4))
    rows = 0
    for _ in range(paths):
        values = [rng.normal() for _ in range(T + 1)]
        result = incumbent_gate(values[:T], values[T])
        admitted += int(bool(result["admitted"]))
        beta_sum += float(result.get("beta", 0.0))
        t_sum += float(result.get("t_reversion", 0.0))
        z_gate += int(abs(float(result.get("z", 0.0))) >= 0.60)
        rows = int(result["rows"])
    rate = admitted / paths
    return {
        "process": "iid_standardized_residual_returns_no_predictability",
        "T": T,
        "H": H,
        "regression_rows": rows,
        "paths": paths,
        "admitted": admitted,
        "admission_rate": rate,
        "mean_beta": beta_sum / paths,
        "mean_t_reversion": t_sum / paths,
        "residual_z_gate_rate": z_gate / paths,
        "independence_scale_expected_admissions_at_350_markets": 350.0 * rate,
        "independence_scale_probability_at_least_one_at_350_markets": 1.0 - (1.0 - rate) ** 350,
    }


def source_contract(root: Path) -> dict[str, object]:
    engine = (root / "src" / "engine.cpp").read_text(encoding="utf-8")
    config = json.loads((root / "config" / "paper_v5.json").read_text(encoding="utf-8"))
    pca = next(item for item in config["multi_strategy"]["strategies"] if item["name"] == "pca")
    required_engine_fragments = [
        "for (std::size_t k = t + 1 - H; k <= t; ++k) state += resid[k][j];",
        "next_resid.push_back(resid[t + 1][j]);",
        "const double state_mu = mean(states);",
        "const double y_mu = mean(next_resid);",
        "const double beta = sxy / (sxx + ridge);",
        "if (t_reversion < 0.75) continue;",
    ]
    return {
        "engine_contract_present": all(fragment in engine for fragment in required_engine_fragments),
        "required_engine_fragments": required_engine_fragments,
        "pca_capital_fraction": float(pca["capital_fraction"]),
        "pca_min_history": int(pca["overrides"]["pca_min_history"]),
        "pca_window": int(pca["overrides"]["pca_window"]),
        "pca_universe": int(pca["overrides"]["pca_universe"]),
        "pca_min_residual_z": float(pca["overrides"]["pca_min_residual_z"]),
    }


def summarize(root: Path, paths: int = 400) -> dict[str, object]:
    experiments = [iid_null_experiment(T, paths=paths) for T in (23, 48, 96, 720)]
    return {
        "schema": "polymarket_lf_v5_pca_state_bias_audit_v1",
        "source_contract": source_contract(root),
        "experiments": experiments,
        "mechanism": (
            "Each training row predicts residual[t+1] from an H-period rolling residual state ending at t. "
            "Although same-row covariance is zero under iid residual returns, residual[t+1] enters later "
            "overlapping state rows. Demeaning the full overlapping design therefore induces a finite-sample "
            "negative covariance in the incumbent regression, mechanically favoring beta<0."
        ),
        "interpretation": (
            "The deterministic iid fixtures contain no mean-reversion predictability, yet the full incumbent "
            "V5 PCA adjustment gate admits a material fraction of paths. The 350-market multiplicity numbers "
            "are independence-scale diagnostics only, not estimates of live Polymarket false discoveries or PnL."
        ),
        "required_experiment": [
            "use timestamp-aligned residual returns as required by LF PR #174",
            "compare incumbent overlapping-state regression with purged/non-overlapping and block-bootstrap calibrated alternatives on identical chronological residual histories",
            "apply market/event/time-cluster multiplicity control across the searched PCA universe",
            "report candidate survival, Brier/log-loss where terminal probabilities are used, and executable OOS PnL/drawdown at 1x/1.5x/2x costs",
            "evaluate jointly with confidence-aware singleton decisions from LF PR #141 rather than letting low reversion confidence cancel in the one-expert book",
        ],
        "decision": "MORE_EVIDENCE_REQUIRED",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--paths", type=int, default=400)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    print(json.dumps(summarize(root, max(1, args.paths)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
