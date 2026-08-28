#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_fair_value_research import (  # noqa: E402
    FairObservation,
    evaluate_model_ladder,
    expanding_folds,
    fit_platt,
    learning_curve,
    score,
    walk_forward_predictions,
)


def rows() -> list[FairObservation]:
    out: list[FairObservation] = []
    timestamp = 1_000_000_000
    for contract in range(36):
        outcome = 1 if contract % 2 == 0 else 0
        base = 0.72 if outcome else 0.28
        for tte in (120.0, 30.0):
            p = base + (0.03 if tte == 30.0 and outcome else -0.03 if tte == 30.0 else 0.0)
            p = min(0.95, max(0.05, p))
            out.append(FairObservation(
                contract_id=f"c{contract:03d}",
                market_handle=contract + 1,
                rules_hash="a" * 64,
                contract_version=1,
                reference_version=1,
                timestamp_ns=timestamp,
                day=f"2026-08-{1 + contract // 4:02d}",
                tte_seconds=tte,
                pm_mid=0.60 if outcome else 0.40,
                oracle_only_probability=0.62 if outcome else 0.38,
                external_median_probability=0.66 if outcome else 0.34,
                structural_probability=0.69 if outcome else 0.31,
                full_external_probability=p,
                lower_probability=max(0.0, p - 0.08),
                upper_probability=min(1.0, p + 0.08),
                outcome=outcome,
                causal_cut_id=contract * 2 + (1 if tte == 120.0 else 2),
                max_input_receive_ns=timestamp - 1,
                model_version="shadow-v1",
            ))
            timestamp += 1_000_000
    return out


def main() -> None:
    data = rows()
    for row in data:
        row.validate()

    model = fit_platt(data[:40])
    assert model.training_contracts == 20
    assert model.training_observations == 40
    assert model.slope > 0.0

    raw_score = score(data, [row.full_external_probability for row in data])
    assert raw_score.contracts == 36
    assert raw_score.log_loss > 0.0
    assert raw_score.brier > 0.0

    folds = expanding_folds(data, min_train_contracts=12, validation_contracts=6, embargo_ns=0)
    assert len(folds) >= 3
    previous_training = 0
    for train, validation in folds:
        train_ids = {row.contract_id for row in train}
        val_ids = {row.contract_id for row in validation}
        assert train_ids.isdisjoint(val_ids)
        assert len(train_ids) >= previous_training
        previous_training = len(train_ids)
        assert max(row.timestamp_ns for row in train) < min(row.timestamp_ns for row in validation)

    oos_rows, probabilities, diagnostics = walk_forward_predictions(
        data,
        source="full_external_probability",
        min_train_contracts=12,
        validation_contracts=6,
    )
    assert oos_rows
    assert len(oos_rows) == len(probabilities)
    assert diagnostics
    assert all(0.0 < p < 1.0 for p in probabilities)

    ladder = evaluate_model_ladder(data, min_train_contracts=12, validation_contracts=6)
    assert set(ladder["models"]) == {
        "PM Mid", "Oracle Only", "External Median", "Structural", "Full External"
    }
    assert ladder["models"]["Full External"]["scores"]["contracts"] > 0

    curve = learning_curve(data, minimum=10, step=10)
    assert curve
    assert curve[0]["contracts"] == 10


if __name__ == "__main__":
    main()
