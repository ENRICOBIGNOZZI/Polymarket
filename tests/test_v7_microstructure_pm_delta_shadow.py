from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from collections import defaultdict, deque
from pathlib import Path
from types import SimpleNamespace
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v7_microstructure_pm_delta_shadow as shadow

SHA = "a" * 40
MODEL_HASH = ""


def stable(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def artifact_value():
    value = {
        "schema": shadow.ARTIFACT_SCHEMA,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
        "automatic_promotion": False,
        "target": "PM_YES_DELTA_250MS_CAUSAL_BOOK",
        "horizon_ms": 250,
        "feature_names": list(shadow.FEATURE_NAMES),
        "source_model_sha": "b" * 40,
        "coefficients_raw": {
            "intercept": 0.000043,
            "slopes": {
                "depth_imbalance_l1": 0.002683,
                "book_imbalance_l5": 0.002765,
            },
        },
    }
    value["model_hash"] = shadow._artifact_model_hash(value)
    return value


def cut(token, sequence, *, bid=0.49, ask=0.51, bid_depth=60.0,
        ask_depth=40.0, imbalance=0.3, received=1000):
    return {
        "market_id": "m1", "token_id": token,
        "observer_sequence": sequence, "receive_wall_ms": received,
        "best_bid": bid, "best_ask": ask, "tick_size": 0.01,
        "bid_depth_l1": bid_depth, "ask_depth_l1": ask_depth,
        "placement_features": {"imbalance": imbalance},
        "valid": True, "lineage_continuous": True,
    }


class MicrostructureShadowTests(unittest.TestCase):
    def test_artifact_hash_and_safety_contract(self):
        value = artifact_value()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            file_hash = shadow.file_sha256(path)
            loaded = shadow.load_artifact(path, file_hash, value["model_hash"])
            self.assertEqual(loaded["model_hash"], value["model_hash"])
            value["execution_authority"] = "MAKE"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "artifact_contract"):
                shadow.load_artifact(path, shadow.file_sha256(path), value["model_hash"])

    def test_score_formula_is_exact_and_zero_authority(self):
        model = artifact_value()
        yes = cut("yes", 10, bid_depth=75.0, ask_depth=25.0, imbalance=0.2)
        no = cut("no", 9, bid=0.49, ask=0.51, bid_depth=25.0,
                 ask_depth=75.0, imbalance=-0.2)
        row = shadow.score_cuts(
            yes, no, model, runtime_model_sha=SHA,
            artifact_file_sha256="c" * 64, scored_wall_ns=1_001_000_000,
        )
        expected = 0.000043 + 0.002683 * 0.5 + 0.002765 * 0.2
        self.assertAlmostEqual(row["predicted_delta_probability"], expected)
        self.assertEqual(row["execution_authority"], "ZERO_AUTHORITY_RESEARCH_ONLY")
        self.assertFalse(row["automatic_promotion"])
        self.assertFalse(row["real_order_submission"])
        self.assertEqual(row["features"]["depth_imbalance_l1"], 0.5)
        self.assertEqual(row["features"]["book_imbalance_l5"], 0.2)

    def test_sequence_causality_rejects_later_same_timestamp_cut(self):
        book = SimpleNamespace(history=defaultdict(deque))
        history = book.history[("m1", "no")]
        history.append(cut("no", 9, received=1000))
        history.append(cut("no", 11, received=1000))
        row = shadow.asof_sequence(book, "m1", "no", 1000, 10)
        self.assertIsNotNone(row)
        self.assertEqual(row["observer_sequence"], 9)
        self.assertIsNone(shadow.asof_sequence(book, "m1", "no", 999, 10))

    def test_complement_inconsistency_fails_closed(self):
        yes = cut("yes", 10, bid=0.70, ask=0.72)
        no = cut("no", 9, bid=0.50, ask=0.52)
        with self.assertRaisesRegex(ValueError, "complement_inconsistent"):
            shadow.paired_probability(yes, no)


if __name__ == "__main__":
    unittest.main()
