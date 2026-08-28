#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_fair_dataset import (  # noqa: E402
    DatasetError,
    ForecastObservation,
    SettlementEvidence,
    build_dataset,
)


def forecast(contract: str, market: int, tte: float, mono: int, wall: int) -> ForecastObservation:
    return ForecastObservation(
        contract_id=contract,
        market_handle=market,
        rules_hash="a" * 64,
        contract_version=11,
        reference_version=12,
        asset="BTC",
        contract_start_ns=1,
        contract_end_ns=1_000_000,
        observation_monotonic_ns=mono,
        observation_wall_ns=wall,
        tte_seconds=tte,
        features={"external_return_250ms": 0.001},
        feature_source_versions={"external": 3, "oracle": 4, "pm": 5},
        feature_receive_times_ns={"external": mono - 3, "oracle": mono - 2, "pm": mono - 1},
        oracle_state_version=4,
        external_state_version=3,
        pm_state_version=5,
        causal_cut_id=100 + mono,
        max_input_receive_monotonic_ns=mono - 1,
        pm_mid=0.50,
        oracle_only_probability=0.52,
        external_median_probability=0.54,
        structural_probability=0.56,
        calibrated_probability=0.57,
        full_external_probability=0.58,
        lower_probability=0.50,
        upper_probability=0.66,
        model_family="structural_bridge",
        model_version="v1",
        model_hash="b" * 64,
        feature_schema_version="f1",
    )


def settlement(contract: str, market: int, outcome: int = 1) -> SettlementEvidence:
    return SettlementEvidence(
        contract_id=contract,
        market_handle=market,
        rules_hash="a" * 64,
        contract_version=11,
        reference_version=12,
        outcome=outcome,
        settlement_source="Chainlink BTC/USD TWAP",
        terminal_oracle_exact="65001.00000000",
        terminal_oracle_numeric=65001.0,
        opening_reference_exact="65000.00000000",
        opening_reference_numeric=65000.0,
        settlement_observation_wall_ns=100_000,
        settlement_receive_monotonic_ns=50_000,
        provenance_hash="c" * 64,
        verified=True,
    )


def must_fail(fn, contains: str) -> None:
    try:
        fn()
    except DatasetError as exc:
        assert contains in str(exc), str(exc)
    else:
        raise AssertionError("expected DatasetError")


def main() -> None:
    forecasts = [
        forecast("c1", 1, 121.0, 100, 1_000),
        forecast("c1", 1, 120.2, 110, 1_100),  # nearest for 120s bucket
        forecast("c1", 1, 30.1, 120, 1_200),
        forecast("c2", 2, 119.9, 130, 1_300),
        # eligible observation even though no trade field exists anywhere.
        forecast("unresolved", 3, 120.0, 140, 1_400),
    ]
    rows = build_dataset(forecasts, [settlement("c1", 1), settlement("c2", 2, 0)])
    assert len(rows) == 3
    assert {row.contract_id for row in rows} == {"c1", "c2"}
    c1 = [row for row in rows if row.contract_id == "c1"]
    assert len(c1) == 2
    assert sorted(row.tte_bucket_seconds for row in c1) == [30.0, 120.0]
    nearest = next(row for row in c1 if row.tte_bucket_seconds == 120.0)
    assert nearest.observation_monotonic_ns == 110
    assert abs(nearest.sample_weight - 0.5) < 1e-12
    c2 = next(row for row in rows if row.contract_id == "c2")
    assert c2.sample_weight == 1.0
    assert c2.terminal_outcome == 0
    assert abs(sum(row.sample_weight for row in rows) - 2.0) < 1e-12

    bad_settlement = settlement("c1", 99)
    must_fail(lambda: build_dataset([forecast("c1", 1, 120.0, 100, 1_000)], [bad_settlement]),
              "lineage_mismatch")

    future = forecast("c1", 1, 120.0, 100, 1_000)
    future = ForecastObservation(**{
        **future.__dict__,
        "feature_receive_times_ns": {"external": 101, "oracle": 99, "pm": 99},
    })
    must_fail(future.validate, "feature_receive_future")

    conflicting = SettlementEvidence(**{
        **settlement("c1", 1).__dict__,
        "outcome": 0,
    })
    must_fail(
        lambda: build_dataset([forecast("c1", 1, 120.0, 100, 1_000)],
                              [settlement("c1", 1), conflicting]),
        "conflicting_evidence",
    )


if __name__ == "__main__":
    main()
