from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_external_economic_common import canonical_sha256  # noqa: E402
from v7_external_settlement_dataset import build_dataset  # noqa: E402
from v7_external_settlement_model import predict, runtime_features  # noqa: E402
from v7_external_settlement_train import train_artifact  # noqa: E402
from v7_external_settlement_validate import (  # noqa: E402
    _calibration_bins, _economic_actions, validate,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _source_tapes(root: Path) -> None:
    common = {
        "schema": "polymarket_v7_external_fair_counterfactual_v1",
        "model_sha": "a" * 40, "policy_sha256": "b" * 64,
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "execution_authority": "SHADOW_ZERO_AUTHORITY",
        "forecast_id": "forecast-1", "market_id": "market-1",
    }
    forecast = {
        **common, "record_id": "forecast-record", "event_type": "FORECAST",
        "event_id": "event-1", "rules_hash": "c" * 64,
        "reference_version": 1000, "observed_ms": 1_100_000,
        "observed_tte_seconds": 200.0, "market_yes": 0.55,
        "yes_best_bid": 0.54, "yes_best_ask": 0.56,
        "no_best_bid": 0.44, "no_best_ask": 0.46,
        "yes_best_bid_visible_size": 10.0, "yes_best_ask_visible_size": 10.0,
        "no_best_bid_visible_size": 10.0, "no_best_ask_visible_size": 10.0,
        "yes_min_order_size": 5.0, "no_min_order_size": 5.0,
        "fee_schedule": {"rate": 0.07, "exponent": 1},
        "external_features": {
            "composite_price": 100.6, "return_1s": 0.001,
            "return_5s": 0.002, "age_ns": 400_000_000,
        },
    }
    final = {
        **common, "record_id": "final-record", "event_type": "FORECAST_FINAL",
        "actual_yes": 1.0, "timestamp_ms": 1_301_000,
    }
    _write_jsonl(root / "run" / "external_fair" / "counterfactuals.jsonl", [forecast, final])

    def event(topic: str, source_ms: int, receive_ms: int, price: float) -> dict:
        return {
            "schema": "polymarket_v7_rtds_price_event_v1", "topic": topic,
            "timestamp_ms": source_ms, "receive_wall_ns": receive_ms * 1_000_000,
            "price": price, "paper_only": True, "authenticated_execution": False,
            "real_order_submission": False,
        }

    oracle = [
        event("crypto_prices_twap_sixty", 1_000_000, 1_000_100, 100.0),
        event("crypto_prices_twap_sixty", 1_099_000, 1_099_100, 100.5),
        event("crypto_prices_twap_sixty", 1_300_000, 1_300_100, 101.0),
    ]
    external = [
        event("crypto_prices", 1_069_000, 1_069_100, 100.1),
        event("crypto_prices", 1_094_000, 1_094_100, 100.3),
        event("crypto_prices", 1_098_000, 1_098_100, 100.4),
        event("crypto_prices", 1_099_500, 1_099_600, 100.6),
    ]
    _write_jsonl(root / "run" / "external_fair" / "rtds_events.jsonl", [*oracle, *external])


def _rehash(row: dict) -> dict:
    output = dict(row)
    output.pop("row_sha256", None)
    output["row_sha256"] = canonical_sha256(output)
    return output


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _source_tapes(root)
        rows, manifest = build_dataset([root])
        assert manifest["fail_closed"] is False
        assert manifest["rows"] == 1
        assert manifest["independent_contracts"] == 1
        row = rows[0]
        assert abs(row["target_settlement_margin_bps"] - 100.0) < 1e-9
        assert row["actual_yes"] == 1.0
        assert row["causality_valid"] is True
        assert row["training_serving_feature_parity"] is True
        assert row["settlement_window_decomposition"]["known_component_available"] is False

        training_rows: list[dict] = []
        for index in range(12):
            clone = json.loads(json.dumps(row))
            clone["forecast_id"] = f"forecast-{index}"
            clone["market_id"] = f"market-{index:02d}"
            clone["observed_ms"] = 1_100_000 + index * 86_400_000
            clone["observed_day"] = clone["observed_ms"] // 86_400_000
            margin = 20.0 if index % 2 == 0 else -20.0
            clone["target_settlement_margin_bps"] = margin
            clone["actual_yes"] = float(margin >= 0.0)
            clone["features"]["oracle_minus_reference_bps"] = margin * 0.5
            training_rows.append(_rehash(clone))

        artifact, report = train_artifact(
            training_rows, code_sha="a" * 40, policy_version="b" * 64,
            dataset_sha256="d" * 64, ridge=1.0, minimum_contracts=3,
        )
        artifact.validate()
        assert report["split_contracts"] == {"train": 7, "validation": 2, "test": 3}
        inference = predict(artifact, training_rows[-1]["features"])
        assert 0.0 < inference["lower"] <= inference["yes"] <= inference["upper"] < 1.0
        assert inference["settlement_sigma_bps"] > 0.0

        runtime = runtime_features(
            tte_seconds=90.0, reference_price=100.0, oracle_price=100.1,
            external={
                "composite_price": 100.2, "return_1s": 0.001,
                "return_5s": 0.002, "age_ns": 1_000_000,
            }, oracle_age_ns=2_000_000,
        )
        assert runtime is not None
        assert set(runtime) == set(artifact.parameters["feature_names"])

        config = {
            "taker": {
                "minimum_robust_ev_per_share": 0.001,
                "base_execution_risk_per_share": 0.0005,
            },
            "promotion": {
                "minimum_settlement_labeled_days": 30,
                "minimum_settlement_labeled_contracts": 2500,
                "minimum_policy_forward_oos_trades": 300,
            },
        }
        no_forward = validate(artifact, training_rows, config)
        assert no_forward["forward_rows"] == 0
        assert no_forward["promotion_eligible"] is False
        future = json.loads(json.dumps(training_rows[-1]))
        future["market_id"] = "future-market"
        future["observed_ms"] = artifact.hyperparameters["forward_oos_starts_after_ns"] // 1_000_000 + 1
        future["observed_day"] = future["observed_ms"] // 86_400_000
        future = _rehash(future)
        forward = validate(artifact, [*training_rows, future], config)
        assert forward["forward_rows"] == 1
        assert forward["promotion_eligible"] is False

        clustered = _calibration_bins([
            {"market_id": "same", "yes": 0.9, "actual_yes": 1.0, "tte_seconds": 45.0},
            {"market_id": "same", "yes": 0.91, "actual_yes": 1.0, "tte_seconds": 45.0},
            {"market_id": "other", "yes": 0.9, "actual_yes": 1.0, "tte_seconds": 45.0},
        ])
        assert clustered[0]["contracts"] == 2
        assert clustered[0]["observed_rate_wilson95_radius"] > 0.0

        repeated_sources = [json.loads(json.dumps(future)) for _ in range(2)]
        repeated_sources[1]["observed_ms"] += 1
        repeated_evaluations = [{
            "market_id": "future-market", "lower": 0.9, "upper": 0.95,
            "actual_yes": 1.0,
        } for _ in range(2)]
        actions, reasons = _economic_actions(
            repeated_sources, repeated_evaluations, config,
        )
        assert len(actions) == 1
        assert actions[0]["quantity"] == 5.0
        assert reasons["MARKET_POSITION_ALREADY_SELECTED"] == 1


if __name__ == "__main__":
    main()
