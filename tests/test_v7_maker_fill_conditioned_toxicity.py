from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "v7_maker_fill_conditioned_toxicity",
    ROOT / "scripts" / "v7_maker_fill_conditioned_toxicity.py",
)
assert SPEC and SPEC.loader
tox = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tox
SPEC.loader.exec_module(tox)
SHA = "a" * 40


def order(order_id: str, cluster: str, ts: int, *, imbalance: float, probe: bool = False) -> dict:
    return {
        "event_type": "ORDER_SUBMITTED",
        "model_sha": SHA,
        "order_id": order_id,
        "event_id": cluster,
        "market_id": "m-" + cluster,
        "side": "BUY",
        "recorded_ts_ms": ts,
        "metadata": {
            "component": "professional_maker",
            "execution_semantics_version": tox.SEMANTICS,
            "outcome": "YES",
            "placement_action": "JOIN",
            "paper_bootstrap_probe": probe,
            "placement_features_source": "CANONICAL_MAKER_LANE_OBSERVED_FLOW_V1",
            "placement_features_snapshot_id": f"snap-{order_id}",
            "placement_features": {
                "microstructure_shadow_delta_250ms": 0.001 * imbalance,
                "imbalance": imbalance,
                "ofi": 0.25 * imbalance,
                "cancel_intensity": 0.2,
                "trade_intensity": 0.3,
                "short_return_ticks": 0.1 * imbalance,
                "ew_vol_ticks": 0.4,
                "aggressive_sell_prints_per_second": 0.8,
                "spread_ticks": 1.0,
                "distance_from_touch_ticks": 0.0,
                "local_latency_ms": 0.15,
            },
            "execution_alpha": {
                "features": {"queue_ahead": 40.0 + imbalance},
                "fill_probability": {"point": 0.12},
            },
        },
    }


def fill(order_id: str, fill_id: str, ts: int, shares: float = 2.0) -> dict:
    return {
        "event_type": "FILL", "model_sha": SHA, "order_id": order_id,
        "fill_id": fill_id, "recorded_ts_ms": ts, "filled_size": shares,
    }


def markout(order_id: str, fill_id: str, ts: int, value: float) -> dict:
    return {
        "event_type": "MARKOUT", "model_sha": SHA, "order_id": order_id,
        "fill_id": fill_id, "recorded_ts_ms": ts, "markouts": {"250ms": value},
    }


class ToxicityResearchTests(unittest.TestCase):
    def test_feature_cut_uses_order_time_metadata_only(self) -> None:
        row = order("o1", "e1", 1000, imbalance=0.75)
        cut = tox.feature_cut(row)
        self.assertAlmostEqual(cut["imbalance"], 0.75)
        self.assertAlmostEqual(cut["ofi"], 0.1875)
        self.assertAlmostEqual(cut["queue_ahead"], 40.75)
        self.assertAlmostEqual(cut["fill_probability"], 0.12)
        self.assertAlmostEqual(cut["microstructure_shadow_delta_250ms"], 0.00075)

    def test_order_fill_markout_join_is_strictly_forward_and_probe_excluded(self) -> None:
        rows = [
            order("good", "e1", 1000, imbalance=-1.0),
            fill("good", "f1", 1010), markout("good", "f1", 1300, 0.02),
            order("probe", "e2", 2000, imbalance=1.0, probe=True),
            fill("probe", "f2", 2010), markout("probe", "f2", 2300, -0.03),
            order("badtime", "e3", 3000, imbalance=1.0),
            fill("badtime", "f3", 2999), markout("badtime", "f3", 3300, -0.04),
        ]
        joined, diag = tox.build_fill_rows(
            rows, markout_horizon="250ms", placement_actions={"JOIN"},
            include_bootstrap_probes=False,
        )
        self.assertEqual(len(joined), 1)
        self.assertEqual(joined[0]["order_id"], "good")
        self.assertEqual(joined[0]["adverse"], 0)
        self.assertEqual(joined[0]["fill_delay_ms"], 10)
        self.assertEqual(diag["fills_invalid_timing"], 1)

    def test_chronological_cluster_split_is_disjoint(self) -> None:
        rows = []
        for index in range(20):
            rows.append({"event_cluster": f"e{index:02d}", "order_ts_ms": 1000 + index})
        train, validation, test, split = tox.chronological_cluster_split(rows)
        train_ids, val_ids, test_ids = map(set, (split["train"], split["validation"], split["test"]))
        self.assertFalse(train_ids & val_ids)
        self.assertFalse(train_ids & test_ids)
        self.assertFalse(val_ids & test_ids)
        self.assertEqual(len(train) + len(validation) + len(test), len(rows))
        self.assertLess(max(row["order_ts_ms"] for row in train), min(row["order_ts_ms"] for row in validation))
        self.assertLess(max(row["order_ts_ms"] for row in validation), min(row["order_ts_ms"] for row in test))

    def test_predictive_microstructure_survives_disjoint_test(self) -> None:
        rows = []
        for cluster in range(40):
            for adverse, imbalance in ((0, -1.5), (1, 1.5)):
                rows.append({
                    "event_cluster": f"e{cluster:03d}",
                    "order_ts_ms": 10_000 + cluster * 100 + adverse,
                    "filled_shares": 2.0,
                    "markout_per_share": -0.02 if adverse else 0.02,
                    "adverse": adverse,
                    "features": {name: 0.0 for name in tox.FEATURES},
                })
                rows[-1]["features"]["imbalance"] = imbalance
                rows[-1]["features"]["microstructure_shadow_delta_250ms"] = imbalance * 0.001
        train, validation, test, _ = tox.chronological_cluster_split(rows)
        prep = tox.fit_preprocessor(train)
        logistic = tox.fit_logistic(train, prep, ridge=1.0, iterations=2000)
        linear = tox.fit_ridge_markout(train, prep, ridge=1.0)
        metrics = tox.summarize(test, prep, logistic, linear)
        self.assertGreater(metrics["auc"], 0.95)
        threshold, _ = tox.choose_safe_threshold(validation, prep, logistic, 0.25)
        selected = tox.selected_metrics(test, prep, logistic, threshold)
        self.assertGreater(selected["coverage"], 0.20)
        self.assertGreater(selected["share_weighted_markout_per_share"], 0.0)
        self.assertLess(selected["adverse_rate"], 0.25)

    def test_main_fails_closed_when_fill_clusters_are_insufficient(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "ledger.jsonl"
            output = root / "report.json"
            rows = [
                order("o1", "e1", 1000, imbalance=-1.0),
                fill("o1", "f1", 1010),
                markout("o1", "f1", 1300, 0.01),
            ]
            evidence.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            argv = [
                "toxicity", "--maker-evidence", str(evidence), "--output", str(output),
                "--minimum-clusters", "3", "--markout-horizon", "250ms",
            ]
            with mock.patch.object(sys, "argv", argv):
                self.assertEqual(tox.main(), 0)
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["state"], "INSUFFICIENT_FILL_CONDITIONED_EVIDENCE")
        self.assertEqual(report["execution_authority"], "ZERO_AUTHORITY_RESEARCH_ONLY")
        self.assertFalse(report["automatic_promotion"])
        self.assertEqual(report["independent_fill_clusters"], 1)


if __name__ == "__main__":
    unittest.main()
