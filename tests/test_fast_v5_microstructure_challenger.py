from __future__ import annotations

import importlib.util
import json
import math
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "hf_v5_microstructure_challenger",
    ROOT / "scripts" / "hf_v5_microstructure_challenger.py",
)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)

PURGED_SPEC = importlib.util.spec_from_file_location(
    "hf_v5_microstructure_purged_eval",
    ROOT / "scripts" / "hf_v5_microstructure_purged_eval.py",
)
assert PURGED_SPEC and PURGED_SPEC.loader
purged = importlib.util.module_from_spec(PURGED_SPEC)
sys.modules[PURGED_SPEC.name] = purged
PURGED_SPEC.loader.exec_module(purged)


class V5MicrostructureSemanticsTest(unittest.TestCase):
    def test_mirrored_binary_book_has_no_incumbent_taker_edge(self) -> None:
        result = mod.mirrored_binary_invariant(
            yes_bid=0.48,
            yes_ask=0.50,
            yes_bid_depth=100.0,
            yes_ask_depth=50.0,
        )
        self.assertLess(result["complement_error"], 1e-12)
        self.assertTrue(result["no_positive_taker_edge"])
        self.assertLessEqual(result["yes_taker_gross_edge"], 0.0)
        self.assertLessEqual(result["no_taker_gross_edge"], 0.0)
        self.assertGreaterEqual(result["incumbent_q_yes"], 0.48)
        self.assertLessEqual(result["incumbent_q_yes"], 0.50)

    def test_active_v5_micro_is_terminal_probability_semantics(self) -> None:
        source = (ROOT / "src" / "engine.cpp").read_text(encoding="utf-8")
        config = json.loads((ROOT / "config" / "paper_v5.json").read_text(encoding="utf-8"))
        micro = next(item for item in config["multi_strategy"]["strategies"] if item["name"] == "micro")
        self.assertEqual(micro["expert"], "micro")
        self.assertEqual(micro["overrides"]["interval_seconds"], 10)
        self.assertEqual(micro["overrides"]["uncertainty_penalty"], 0.0)
        self.assertIn('out.push_back({"micro", std::clamp(q, 0.001, 0.999), conf});', source)
        self.assertIn("void Engine::score_resolved", source)
        self.assertIn("const double loss = (q - *m.resolved_yes) * (q - *m.resolved_yes);", source)
        self.assertIn("s.gross_edge = qside - ask;", source)

    def test_future_label_uses_only_later_mid(self) -> None:
        rows = []
        for i in range(8):
            rows.append(
                {
                    "exchange_ts_ms": i * 1000,
                    "market_id": "m1",
                    "mid": 0.50 + 0.001 * i,
                    "spread": 0.02,
                    "microprice": 0.501 + 0.001 * i,
                    "imbalance_l1": 0.2,
                    "imbalance_l3": 0.1,
                    "imbalance_l5": 0.05,
                    "ofi_l1": 1.0,
                    "feed_latency_ms": 50.0,
                }
            )
        labeled = mod.label_future_markout(rows, horizon_ms=2000, max_lag_ms=0)
        self.assertEqual(len(labeled), 6)
        self.assertAlmostEqual(labeled[0].future_move, 0.002, places=12)
        self.assertAlmostEqual(labeled[0].features[0], 0.001, places=12)

    def test_capture_replay_preserves_receive_latency_and_prearrival_ofi(self) -> None:
        book = {
            "received_ts_ms": 1787613000050,
            "shard": 0,
            "payload": {
                "event_type": "book",
                "timestamp": 1787613000000,
                "asset_id": "42",
                "bids": [{"price": "0.49", "size": "100"}],
                "asks": [{"price": "0.51", "size": "80"}],
            },
        }
        change = {
            "received_ts_ms": 1787613001055,
            "shard": 0,
            "payload": {
                "event_type": "price_change",
                "timestamp": 1787613001000,
                "price_changes": [
                    {"asset_id": "42", "price": "0.49", "size": "120", "side": "BUY"}
                ],
            },
        }
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = pathlib.Path(directory)
            capture = root / "capture.jsonl"
            features = root / "features.csv"
            capture.write_text(json.dumps(book) + "\n" + json.dumps(change) + "\n", encoding="utf-8")
            rows = mod.replay_capture(capture, features)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["feed_latency_ms"], 50)
            self.assertEqual(rows[1]["feed_latency_ms"], 55)
            self.assertGreater(rows[1]["ofi_l1"], 0.0)
            self.assertTrue(features.is_file())

    def test_purged_split_embargoes_future_label_overlap(self) -> None:
        labeled = []
        for i in range(200):
            labeled.append(
                mod.LabeledRow(
                    ts_ms=i * 1000,
                    market_id="m1",
                    mid=0.5,
                    spread=0.002,
                    features=(0.0, 0.1, 0.1, 0.1, 0.0, 0.002, 50.0),
                    future_move=0.0,
                )
            )
        train, test, cutoff = purged.purged_split(labeled, horizon_ms=5000, tolerance_ms=2500)
        self.assertTrue(train)
        self.assertTrue(test)
        self.assertTrue(all(row.ts_ms + 7500 < cutoff for row in train))
        self.assertTrue(all(row.ts_ms >= cutoff for row in test))

    def test_multifeature_challenger_beats_micro_displacement_fixture(self) -> None:
        labeled = []
        for i in range(240):
            imbalance = math.sin(i * 0.17)
            ofi = math.cos(i * 0.11)
            micro_displacement = 0.00005 * math.sin(i * 0.03)
            spread = 0.0002 + 0.00002 * (i % 3)
            latency = 40.0 + (i % 7)
            y = 0.0018 * imbalance + 0.0012 * ofi + 0.0001 * math.sin(i * 0.07)
            labeled.append(
                mod.LabeledRow(
                    ts_ms=i * 1000,
                    market_id="fixture",
                    mid=0.50,
                    spread=spread,
                    features=(
                        micro_displacement,
                        imbalance,
                        0.8 * imbalance,
                        0.6 * imbalance,
                        ofi,
                        spread,
                        latency,
                    ),
                    future_move=y,
                )
            )
        result = mod.evaluate_challenger(labeled, train_fraction=0.60, l2=0.5, slippage_bps=0.0)
        self.assertEqual(result["test_rows"], 96)
        self.assertLess(result["challenger"]["mse"], 0.15 * result["baseline"]["mse"])
        self.assertGreater(result["challenger"]["sign_accuracy"], 0.80)


if __name__ == "__main__":
    unittest.main()
