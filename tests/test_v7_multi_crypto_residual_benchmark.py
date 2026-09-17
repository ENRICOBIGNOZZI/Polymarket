from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_lead_lag_replay import ReplayError, digest
from v7_multi_crypto_residual_benchmark import build, read_rows, validate_row

BASE = 1_800_000_000_000_000_000
ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB")


def hx(prefix: str, index: int, n: int = 64) -> str:
    return hashlib.sha256(f"{prefix}:{index}".encode()).hexdigest()[:n]


def row(index: int) -> dict:
    decision = BASE + index * 1_000_000_000
    asset = ASSETS[index % len(ASSETS)]
    contract_horizon = "M15" if index % 2 else "M5"
    x = (index % 9 - 4) / 10.0
    p0 = 0.50 + 0.01 * (index % 5 - 2)
    delta = 0.004 * x
    features = {
        "pm_yes_mid": p0, "pm_complete_set_gap": 0.001 * (index % 3),
        "pm_yes_spread": 0.01, "pm_yes_imbalance": x, "tte_seconds": 80.0 + index % 20,
        "distance_to_reference_bp": 2.0 * x, "spot_minus_oracle_bp": x,
        "external": {
            "return_50ms_bp": x, "return_100ms_bp": 2*x, "return_250ms_bp": 3*x,
            "return_1s_bp": 4*x, "dispersion_bps": abs(x), "aggregate_ofi": x,
            "aggregate_trade_imbalance": x/2,
            "shock": {"shock_z_unfloored": 1.5*x, "sigma_100ms_bp_prior": 1.0 + abs(x)},
        },
        "leader_features": {"BTC": {"return_100ms_bp": 1.2*x, "shock_z_unfloored": x}},
        "derivatives": [{"venue": "BINANCE_USDM", "usable": True,
                         "basis_to_spot_bp": x/3, "funding_rate": x/10000}],
    }
    value = {
        "schema": "polymarket_v7_multi_crypto_repricing_labeled_row_v1",
        "model_sha": "a"*40, "policy_hash": "b"*64, "feature_schema_hash": "c"*64,
        "source_identity_hash": hx("source", index), "origin_record_hash": hx("origin", index),
        "asset": asset, "horizon": contract_horizon, "market_id": f"market-{index:03d}",
        "event_id": f"event-{index:03d}", "decision_wall_ns": decision,
        "available_at_ns": decision - 1_000_000, "features": features, "source_versions": {},
        "labels": {"250": {"status": "LABELED", "delta_pm_yes": delta,
                             "target_wall_ns": decision + 250_000_000,
                             "maximum_asof_gap_ms": 50,
                             "asof_gap_ms": 1.0,
                             "label_available_wall_ns": decision + 251_000_000}},
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "execution_authority": False,
    }
    value["row_hash"] = digest(value)
    return value


class MultiCryptoResidualBenchmarkTest(unittest.TestCase):
    def test_all_nested_ablations_use_same_causal_split(self) -> None:
        rows = [row(i) for i in range(72)]
        report = build(rows, label_horizon_ms=250,
                       train_end_ns=BASE+36_000_000_000,
                       validation_end_ns=BASE+54_000_000_000,
                       embargo_ns=500_000_000, ridge=.1,
                       block_ns=5_000_000_000, minimum_clusters=2, bootstrap_draws=50)
        self.assertEqual(report["economic_evidence"], "NOT_PROVEN")
        self.assertFalse(report["automatic_promotion"])
        self.assertEqual(set(report["ablations"]), {"PM_ONLY","OWN_EXTERNAL","ORACLE","LEADERS","DERIVATIVES"})
        counts = [tuple(sorted(value["counts"].items())) for value in report["ablations"].values()]
        self.assertEqual(len(set(counts)), 1)
        for value in report["ablations"].values():
            self.assertTrue(value["feature_selection"]["training_only"])
            self.assertGreater(value["validation"]["rows"], 0)
            self.assertGreater(value["test"]["rows"], 0)
        self.assertIn("open_interest_native", report["excluded_features"])

    def test_future_feature_availability_is_rejected(self) -> None:
        value = row(1); value["available_at_ns"] = value["decision_wall_ns"] + 1; value["row_hash"] = digest({k:v for k,v in value.items() if k != "row_hash"})
        with self.assertRaisesRegex(ReplayError, "FEATURE_AVAILABILITY_INVALID"):
            validate_row(value)

    def test_hash_tampering_is_rejected(self) -> None:
        value = row(1); value["features"]["pm_yes_mid"] = .9
        with self.assertRaisesRegex(ReplayError, "HASH_INVALID"):
            validate_row(value)

    def test_conflicting_duplicate_hash_cannot_be_last_write_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rows.jsonl"
            a = row(1); b = json.loads(json.dumps(a)); b["market_id"] = "other"
            path.write_text(json.dumps(a)+"\n"+json.dumps(b)+"\n")
            with self.assertRaises(ReplayError):
                read_rows([path])


if __name__ == "__main__":
    unittest.main()
