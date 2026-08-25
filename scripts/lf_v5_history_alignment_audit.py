#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
ENGINE_CPP = ROOT / "src" / "engine.cpp"
ENGINE_HPP = ROOT / "include" / "pm" / "engine.hpp"


@dataclass(frozen=True)
class Observation:
    ts: int
    p: float


def logistic(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def logit(p: float) -> float:
    return math.log(p / (1.0 - p))


def corr(x: Iterable[float], y: Iterable[float]) -> float:
    a = list(x)
    b = list(y)
    if len(a) != len(b) or len(a) < 2:
        raise ValueError("correlation requires equal-length samples with at least two points")
    ma = sum(a) / len(a)
    mb = sum(b) / len(b)
    xa = [v - ma for v in a]
    xb = [v - mb for v in b]
    va = sum(v * v for v in xa)
    vb = sum(v * v for v in xb)
    if va <= 0.0 or vb <= 0.0:
        raise ValueError("correlation is undefined for zero-variance samples")
    return sum(u * v for u, v in zip(xa, xb)) / math.sqrt(va * vb)


def positional_logit_returns(obs: list[Observation]) -> list[float]:
    return [logit(obs[i].p) - logit(obs[i - 1].p) for i in range(1, len(obs))]


def exact_bucket_logit_returns(obs: list[Observation], bucket_seconds: int) -> dict[int, float]:
    by_ts = {o.ts: o.p for o in obs}
    out: dict[int, float] = {}
    for ts in sorted(by_ts):
        prev = ts - bucket_seconds
        if prev in by_ts:
            out[ts] = logit(by_ts[ts]) - logit(by_ts[prev])
    return out


def source_contract() -> dict[str, bool]:
    cpp = ENGINE_CPP.read_text(encoding="utf-8")
    hpp = ENGINE_HPP.read_text(encoding="utf-8")
    return {
        "history_container_drops_timestamps": (
            "std::unordered_map<std::string,std::deque<double>> history_;" in hpp
        ),
        "history_csv_persists_timestamp": (
            'ensure("history.csv", "timestamp,market_id,mid")' in cpp
            and "f << ts << ',' << csv_escape(m.id) << ',' << mid" in cpp
        ),
        "load_state_discards_timestamp": (
            "auto& d = history_[x[1]];" in cpp
            and "d.push_back(std::stod(x[2]));" in cpp
        ),
        "pca_uses_positional_index_returns": (
            "z.r.push_back(std::clamp(logit(h[start + t + 1]) - logit(h[start + t])" in cpp
        ),
    }


def deterministic_counterexample() -> dict[str, object]:
    # The two latent logit paths are numerically identical by observation index, so
    # the incumbent positional construction reports perfect +1 correlation. Market B
    # missed t=1; on the genuinely common one-bucket intervals 2->3 and 3->4, its
    # returns are the exact opposite of market A, so timestamp-aligned correlation is -1.
    a_ts = [0, 1, 2, 3, 4]
    b_ts = [0, 2, 3, 4, 5]
    latent = [0.0, 1.0, 0.0, 1.0, 0.0]
    a = [Observation(t, logistic(x)) for t, x in zip(a_ts, latent)]
    b = [Observation(t, logistic(x)) for t, x in zip(b_ts, latent)]

    a_pos = positional_logit_returns(a)
    b_pos = positional_logit_returns(b)
    positional_corr = corr(a_pos, b_pos)

    a_exact = exact_bucket_logit_returns(a, 1)
    b_exact = exact_bucket_logit_returns(b, 1)
    common_endpoints = sorted(set(a_exact).intersection(b_exact))
    a_common = [a_exact[t] for t in common_endpoints]
    b_common = [b_exact[t] for t in common_endpoints]
    exact_corr = corr(a_common, b_common)

    return {
        "market_a_timestamps": a_ts,
        "market_b_timestamps": b_ts,
        "positional_returns_a": a_pos,
        "positional_returns_b": b_pos,
        "positional_correlation": positional_corr,
        "exact_common_interval_endpoints": common_endpoints,
        "exact_returns_a": a_common,
        "exact_returns_b": b_common,
        "exact_timestamp_correlation": exact_corr,
        "correlation_sign_flip": positional_corr > 0.999 and exact_corr < -0.999,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit V5 PCA history time alignment")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()

    contract = source_contract()
    fixture = deterministic_counterexample()
    payload = {
        "source_contract": contract,
        "counterexample": fixture,
        "finding": (
            "V5 Engine::pca_adjustments aligns each market's last T observations by deque index, "
            "not by timestamp. history.csv stores timestamps, but load_state/history_ discard them. "
            "Missing or asynchronous observations can therefore create spurious cross-market factor "
            "correlations, including a deterministic +1 to -1 sign reversal in the fixture."
        ),
        "research_candidate": [
            "store timestamped history points in the V5 engine",
            "resample to a common configured time grid with a bounded as-of staleness rule",
            "form logit returns only between common bucket endpoints",
            "then apply a PSD-safe missingness-aware covariance estimator and compare against incumbent on identical chronological rows",
        ],
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("V5 PCA history time-alignment audit")
        for key, value in contract.items():
            print(f"{key}={value}")
        print(f"positional_correlation={fixture['positional_correlation']:.6f}")
        print(f"exact_timestamp_correlation={fixture['exact_timestamp_correlation']:.6f}")
        print(f"correlation_sign_flip={fixture['correlation_sign_flip']}")
    return 0 if all(contract.values()) and fixture["correlation_sign_flip"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
