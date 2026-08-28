#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_fair_value_registry import (  # noqa: E402
    FairModelArtifact,
    FairValueRegistry,
    PromotionPolicy,
    RegistryError,
)

SHA = "1" * 40
RULE = "a" * 64


def artifact() -> FairModelArtifact:
    return FairModelArtifact.build(
        family="structural_bridge_platt",
        model_version="fv-1",
        feature_schema_version="fv-schema-1",
        code_sha=SHA,
        policy_version="policy-1",
        artifact_role="CHALLENGER",
        training_start_ns=1,
        training_end_ns=100,
        training_contracts=120,
        training_days=10,
        assets=("BTC",),
        contract_templates=("BTC_USD_UPDOWN_5M",),
        rules_hashes=(RULE,),
        parameters={"calibration_slope": 1.0},
        hyperparameters={"uncertainty_z": 1.64},
        oos_scores={"log_loss": 0.50, "brier": 0.16, "ece": 0.02},
        interval_coverage={"coverage": 0.90},
        economic_replay={"net_pnl": 1.0},
        generated_timestamp_ns=200,
    )


def evidence(**overrides: object) -> dict:
    raw = {
        "oos_contracts": 120,
        "forward_shadow_contracts": 60,
        "ece": 0.02,
        "calibration_slope": 1.0,
        "interval_coverage": 0.90,
        "net_replay_pnl": 1.0,
        "edge_monotonicity_pass": True,
        "causality_failures": 0,
        "forward_shadow_frozen": True,
        "exact_code_sha": SHA,
        "rules_hashes": [RULE],
    }
    raw.update(overrides)
    return raw


def must_fail(fn, contains: str) -> None:
    try:
        fn()
    except RegistryError as exc:
        assert contains in str(exc), (contains, str(exc))
    else:
        raise AssertionError(f"expected RegistryError containing {contains!r}")


def main() -> None:
    model = artifact()
    model.validate()
    assert model.with_role("CHAMPION").model_hash == model.model_hash

    with tempfile.TemporaryDirectory() as tmp:
        registry = FairValueRegistry(Path(tmp))
        challenger_pointer = registry.publish_challenger(model)
        challenger = json.loads(challenger_pointer.read_text(encoding="utf-8"))
        assert challenger["role"] == "CHALLENGER"
        assert challenger["model_hash"] == model.model_hash
        assert not registry.champion_pointer.exists()

        must_fail(
            lambda: registry.promote(model, evidence=evidence(forward_shadow_contracts=0)),
            "insufficient_forward_shadow",
        )
        must_fail(
            lambda: registry.promote(model, evidence=evidence(net_replay_pnl=-1.0)),
            "nonpositive_net_replay_pnl",
        )
        must_fail(
            lambda: registry.promote(model, evidence=evidence(causality_failures=1)),
            "causality_failure",
        )
        must_fail(
            lambda: registry.promote(model, evidence=evidence(exact_code_sha="2" * 40)),
            "sha_mismatch",
        )
        must_fail(
            lambda: registry.promote(model, evidence=evidence(rules_hashes=["b" * 64])),
            "rules_scope_mismatch",
        )
        must_fail(
            lambda: registry.promote(model, evidence=evidence(interval_coverage=0.50)),
            "interval_coverage",
        )

        champion_pointer = registry.promote(
            model,
            evidence=evidence(),
            policy=PromotionPolicy(
                minimum_oos_contracts=100,
                minimum_forward_shadow_contracts=50,
            ),
        )
        champion = json.loads(champion_pointer.read_text(encoding="utf-8"))
        assert champion["role"] == "CHAMPION"
        assert champion["model_hash"] == model.model_hash

        # A refit is a new artifact and cannot mutate the champion pointer just
        # by being published as challenger.
        refit = FairModelArtifact.build(
            **{
                **{key: value for key, value in model.__dict__.items()
                   if key not in {"model_hash", "model_version", "parameters", "artifact_role"}},
                "model_version": "fv-2",
                "parameters": {"calibration_slope": 0.95},
                "artifact_role": "CHALLENGER",
            }
        )
        registry.publish_challenger(refit)
        champion_after_refit = json.loads(registry.champion_pointer.read_text(encoding="utf-8"))
        assert champion_after_refit["model_hash"] == model.model_hash
        assert refit.model_hash != model.model_hash

        registry.record_rejected(refit, reason="worse chronological OOS log loss")
        research_lines = registry.research_log.read_text(encoding="utf-8").splitlines()
        assert any("rejected:" in line for line in research_lines)


if __name__ == "__main__":
    main()
