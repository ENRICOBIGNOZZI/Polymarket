from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v7_pm_repricing_shadow as shadow  # noqa: E402


def spec() -> dict:
    return {
        "family": "PM_PLUS_EXTERNAL",
        "feature_names": ["pm__pm_logit", "ext__return_100ms_bp"],
        "means": [0.0, 0.0],
        "scales": [1.0, 1.0],
        "coefficients": [0.0, 0.0, 0.0, 1.0, 0.0],
        "ridge": 1.0,
    }


def artifact() -> dict:
    return {
        "schema": shadow.ARTIFACT_SCHEMA,
        "code_sha": "a" * 40,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
        "automatic_promotion": False,
        "models": {"250": {"PM_PLUS_EXTERNAL": spec()}},
    }


def origin() -> dict:
    return {
        "origin_id": "origin-1",
        "market_id": "market-1",
        "yes_token": "yes-token",
        "no_token": "no-token",
        "origin_observed_wall_ns": 1_000_000_000,
        "rich_feature_sha256": "b" * 64,
        "feature_schema_version": "unused-in-pure-test",
        "rich_model_features": {"return_100ms_bp": 0.10},
    }


def evidence() -> dict:
    def cut(token: str) -> dict:
        return {
            "token_id": token,
            "best_bid": 0.49,
            "best_ask": 0.51,
            "tick_size": 0.01,
            "bid_depth_l1": 10.0,
            "ask_depth_l1": 10.0,
            "placement_features": {
                "imbalance": 0.0,
                "ofi": 0.0,
                "short_return_ticks": 0.0,
            },
        }
    return {
        "origin_pm_yes": 0.5,
        "origin_pm_snapshot_id": "10:11",
        "origin_book_cuts": [cut("yes-token"), cut("no-token")],
        "observer_session_id": "session-1",
        "connection_epoch": 1,
    }


def test_artifact_hash_and_safety_contract() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "artifact.json"
        payload = json.dumps(artifact(), sort_keys=True).encode()
        path.write_bytes(payload)
        sha = hashlib.sha256(payload).hexdigest()
        value, model = shadow.load_model(path, sha, 250, "PM_PLUS_EXTERNAL")
        assert value["automatic_promotion"] is False
        assert model["family"] == "PM_PLUS_EXTERNAL"
        try:
            shadow.load_model(path, "0" * 64, 250, "PM_PLUS_EXTERNAL")
        except ValueError as exc:
            assert "artifact_hash_mismatch" in str(exc)
        else:
            raise AssertionError("wrong artifact hash accepted")


def test_frozen_wrapper_artifact_loads_with_source_identity() -> None:
    path = ROOT / "config" / "v7_pm_repricing_250ms_shadow.json"
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    value, model = shadow.load_model(path, sha, 250, "PM_PLUS_EXTERNAL")
    assert value["schema"] == shadow.FROZEN_ARTIFACT_SCHEMA
    assert value["source_code_sha"] == "acbbf12aa5523f7ff646ba1b51fa1d37cd8f0613"
    assert model["family"] == "PM_PLUS_EXTERNAL"


def test_score_is_zero_authority_and_marks_adverse_no_buy() -> None:
    row = shadow.score_origin(
        origin(), evidence(), spec(),
        artifact_sha256="c" * 64,
        artifact_code_sha="a" * 40,
        runtime_sha="d" * 40,
        horizon_ms=250,
        family="PM_PLUS_EXTERNAL",
        threshold_ticks=1.0,
        scored_wall_ns=1_010_000_000,
    )
    assert row["execution_authority"] == "ZERO_AUTHORITY_RESEARCH_ONLY"
    assert row["real_order_submission"] is False
    assert row["predicted_delta_probability"] > 0.01
    assert row["would_veto_yes_buy"] is False
    assert row["would_veto_no_buy"] is True
    assert row["inference_age_ns"] == 10_000_000


if __name__ == "__main__":
    test_artifact_hash_and_safety_contract()
    test_frozen_wrapper_artifact_loads_with_source_identity()
    test_score_is_zero_authority_and_marks_adverse_no_buy()
