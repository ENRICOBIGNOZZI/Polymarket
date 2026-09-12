import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_external_residual_train import train_residual
from v7_external_rich_model import FEATURE_NAMES
from v7_external_rich_train import fit, score


class ResidualOverlayTest(unittest.TestCase):
    def rows(self, n=60):
        out = []
        base = 1_800_000_000_000
        for i in range(n):
            y = float(i % 2)
            p = 0.55 if y else 0.45
            features = {name: None for name in FEATURE_NAMES}
            features[FEATURE_NAMES[0]] = float(i % 7) + (1.0 if y else -1.0)
            features[FEATURE_NAMES[1]] = float((i * 3) % 11)
            start = base + i * 300_000
            out.append({
                "market_id": f"m{i:03d}",
                "forecast_id": f"f{i:03d}",
                "market_start_ms": start,
                "observed_ms": start + 5_000,
                "label_received_ms": start + 60_000,
                "actual": y,
                "market_probability": p,
                "features": features,
                "rules_hash": "b" * 64,
                "origin_record_id": f"o{i}",
                "final_record_id": f"z{i}",
                "source_code_sha": "a" * 40,
            })
        return out

    def protocol(self):
        return {
            "schema": "polymarket_v7_residual_overlay_protocol_v1",
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
            "automatic_promotion": False,
            "model": {
                "allowed_offset": "market",
                "ridge_grid": [1.0, 10.0],
                "correction_shrinkage_grid": [0.0, 0.25, 1.0],
            },
            "prospective": {
                "replication_count": 3,
                "duration_seconds_each": 28800,
            },
        }

    def test_residual_training_cannot_replace_pm_offset(self):
        rows = self.rows()
        artifact, report = train_residual(
            rows, "a" * 40, self.protocol(), [],
            generated_ns=(rows[-1]["label_received_ms"] + 10_000) * 1_000_000,
        )
        self.assertEqual(artifact.parameters["offset"], "market")
        self.assertTrue(artifact.parameters["residual_only"])
        self.assertEqual(report["selected_offset"], "market")
        self.assertEqual(artifact.economic_replay["execution_authority"], "ZERO_AUTHORITY_RESEARCH_ONLY")
        self.assertFalse(report["automatic_promotion"])
        self.assertEqual(artifact.hyperparameters["replication_count"], 3)
        self.assertEqual(artifact.hyperparameters["duration_seconds_each"], 28800)

    def test_zero_shrinkage_is_exact_pm_baseline(self):
        rows = self.rows(20)
        parameters = fit(rows, 10.0, "market")
        parameters = dict(parameters, coefficients=[0.0] * len(parameters["coefficients"]))
        residual = score(rows, parameters)
        baseline = score(rows, None)
        self.assertAlmostEqual(residual["brier"], baseline["brier"], places=15)
        self.assertAlmostEqual(residual["log_loss"], baseline["log_loss"], places=15)

    def test_non_market_offset_fails_closed(self):
        protocol = self.protocol()
        protocol["model"]["allowed_offset"] = "none"
        rows = self.rows()
        with self.assertRaisesRegex(ValueError, "market_offset_required"):
            train_residual(
                rows, "a" * 40, protocol, [],
                generated_ns=(rows[-1]["label_received_ms"] + 10_000) * 1_000_000,
            )


if __name__ == "__main__":
    unittest.main()
