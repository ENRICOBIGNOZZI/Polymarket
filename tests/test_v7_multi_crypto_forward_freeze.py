from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from test_v7_opportunity import multi_forward_envelope
from v7_lead_lag_replay import ReplayError
from v7_multi_crypto_forward_freeze import freeze
from v7_opportunity import OpportunityEnvelope


def draft(asset: str = "ETH", horizon: str = "M5") -> dict:
    return {
        "schema": "polymarket_v7_multi_crypto_forward_protocol_draft_v1",
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "real_capital_at_risk": False,
        "automatic_promotion": False, "research_only": True,
        "experiment_id": f"{asset}-{horizon}-forward-001", "parent_experiment_id": None,
        "code_sha": "a" * 40, "asset": asset, "horizon": horizon,
        "data_cutoff_ns": 3000, "train_end_ns": 1000, "validation_end_ns": 2000,
        "embargo_ns": 100,
        "feature_schema_hash": "b" * 64, "model_hash": "c" * 64,
        "fill_model_hash": "d" * 64, "cost_model_hash": "e" * 64,
        "settlement_semantic_hash": "f" * 64, "latency_profile_id": "london-paper-profile-v1",
        "evidence": {
            "rules_verified": True, "token_mapping_verified": True,
            "oracle_binding_verified": True, "required_feeds_verified": True,
            "pm_book_replay_verified": True, "fee_model_verified": True,
            "fill_model_verified": True, "latency_profile_verified": True,
            "accounting_reconciled": True, "single_writer_verified": True,
            "training_artifact_frozen": True,
        },
        "entry_policy": {
            "signal_definition_hash": "1" * 64, "shock_threshold_z": 2.0,
            "confirmation_rule": "NON_OPPOSING", "minimum_tte_seconds": 30.0,
            "maximum_tte_seconds": 90.0, "maximum_signal_age_ms": 1000,
            "target_shares": 5.0, "one_entry_per_market": True,
            "hold_to_settlement": True, "order_type": "FAK",
            "price_rule": "ARRIVAL_BEST_ASK_NO_CHASE",
            "require_full_visible_depth": True, "entry_uses_absolute_fair": False,
            "probability_source": "POLYMARKET_PRIOR_ONLY",
        },
        "risk_policy": {
            "cohort_maximum_loss_usd": 100.0, "market_loss_cap_usd": 10.0,
            "asset_loss_cap_usd": 25.0, "horizon_loss_cap_usd": 25.0,
            "parent_shock_loss_cap_usd": 25.0, "portfolio_loss_cap_usd": 100.0,
            "global_cash_checkpoint_required": True,
        },
        "statistical_protocol": {
            "primary_endpoint": "NET_PNL_PER_CANDIDATE", "target_independent_markets": 100,
            "minimum_clusters": 20, "bootstrap_block_ns": 60_000_000_000,
            "bootstrap_seed": 17, "bootstrap_draws": 2000,
            "forward_duration_seconds": 7200,
            "stopping_rule": "FIXED_DURATION_AND_MINIMUM_MARKETS_NO_PNL_STOP",
            "no_runtime_tuning": True, "common_time_split_across_assets": True,
        },
    }


class MultiCryptoForwardFreezeTest(unittest.TestCase):
    def test_freeze_is_deterministic_and_has_zero_authority(self) -> None:
        first = freeze(draft()); second = freeze(draft())
        self.assertEqual(first["protocol_hash"], second["protocol_hash"])
        self.assertEqual(first["multi_crypto_forward"], second["multi_crypto_forward"])
        self.assertFalse(first["entry_authority"])
        self.assertFalse(first["automatic_promotion"])
        self.assertFalse(first["real_order_submission"])

    def test_economic_parameter_change_changes_protocol_identity(self) -> None:
        first = freeze(draft())
        changed = draft(); changed["entry_policy"]["shock_threshold_z"] = 2.5
        second = freeze(changed)
        self.assertNotEqual(first["protocol_hash"], second["protocol_hash"])

    def test_incomplete_evidence_and_bad_split_fail_closed(self) -> None:
        value = draft(); value["evidence"]["fee_model_verified"] = False
        with self.assertRaisesRegex(ReplayError, "EVIDENCE_INCOMPLETE"):
            freeze(value)
        value = draft(); value["train_end_ns"] = value["validation_end_ns"]
        with self.assertRaisesRegex(ReplayError, "SPLIT_ORDER_INVALID"):
            freeze(value)

    def test_packet_is_accepted_by_typed_opportunity_contract(self) -> None:
        frozen = freeze(draft(asset="SOL", horizon="M15"))
        value = multi_forward_envelope(asset="SOL", horizon="M15")
        value["multi_crypto_forward"] = frozen["multi_crypto_forward"]
        value["crypto_context"]["settlement_semantic_hash"] = frozen["multi_crypto_forward"]["settlement_semantic_hash"]
        value["mapping_identity"] = frozen["multi_crypto_forward"]["settlement_semantic_hash"]
        value["latency"]["profile_id"] = frozen["multi_crypto_forward"]["latency_profile_id"]
        parsed = OpportunityEnvelope.parse(value)
        self.assertTrue(parsed.is_multi_crypto_forward)

    def test_cli_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "draft.json"; output = root / "frozen.json"
            source.write_text(json.dumps(draft()))
            command = [sys.executable, str(ROOT / "scripts/v7_multi_crypto_forward_freeze.py"),
                       "--input", str(source), "--output", str(output)]
            first = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            original = output.read_bytes()
            second = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(output.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
