from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "v7_maker_prefill_path_toxicity", SCRIPTS / "v7_maker_prefill_path_toxicity.py"
)
assert SPEC and SPEC.loader
pathmod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pathmod
SPEC.loader.exec_module(pathmod)
import v7_maker_fill_conditioned_toxicity as base

SHA = "a" * 40


def maker_order(order_id: str, cluster: str, ts: int, *, imbalance: float = 0.8) -> dict:
    return {
        "event_type": "ORDER_SUBMITTED", "model_sha": SHA, "order_id": order_id,
        "event_id": cluster, "market_id": "m-" + cluster, "token_id": "yes-" + cluster,
        "side": "BUY", "recorded_ts_ms": ts,
        "metadata": {
            "component": "professional_maker",
            "execution_semantics_version": base.SEMANTICS,
            "outcome": "YES", "placement_action": "JOIN", "paper_bootstrap_probe": False,
            "placement_features": {
                "microstructure_shadow_delta_250ms": 0.002,
                "imbalance": imbalance, "ofi": 0.4, "cancel_intensity": 0.1,
                "trade_intensity": 0.2, "short_return_ticks": 0.1,
                "ew_vol_ticks": 0.3, "aggressive_sell_prints_per_second": 0.5,
                "spread_ticks": 1.0, "distance_from_touch_ticks": 0.0,
                "local_latency_ms": 0.1,
            },
            "execution_alpha": {
                "features": {"queue_ahead": 50.0},
                "fill_probability": {"point": 0.2},
            },
        },
    }


def fill(order_id: str, fill_id: str, ts: int) -> dict:
    return {"event_type": "FILL", "model_sha": SHA, "order_id": order_id,
            "fill_id": fill_id, "recorded_ts_ms": ts, "filled_size": 2.0}


def mark(order_id: str, fill_id: str, ts: int, value: float) -> dict:
    return {"event_type": "MARKOUT", "model_sha": SHA, "order_id": order_id,
            "fill_id": fill_id, "recorded_ts_ms": ts, "markouts": {"250ms": value}}


def book(cluster: str, ts: int, *, score: float, imbalance: float,
         ofi: float = 0.0, sell: bool = False) -> dict:
    return {
        "schema": "polymarket_v7_causal_book_observation_v1",
        "model_sha": SHA, "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
        "valid": True, "lineage_continuous": True, "features_valid": True,
        "market_id": "m-" + cluster, "token_id": "yes-" + cluster,
        "receive_wall_ms": ts, "observer_sequence": ts,
        "placement_features": {
            "microstructure_shadow_delta_250ms": score,
            "imbalance": imbalance, "ofi": ofi, "cancel_intensity": 0.2,
            "trade_intensity": 0.3, "aggressive_sell_prints_per_second": 0.8,
        },
        "public_trade": ({"aggressor_side": "SELL", "size": 1.0} if sell else None),
    }


class PrefillPathToxicityTests(unittest.TestCase):
    def test_cancel_latency_cut_excludes_too_late_book_updates(self) -> None:
        maker = [
            maker_order("o1", "e1", 1000),
            fill("o1", "f1", 1100), mark("o1", "f1", 1400, -0.02),
        ]
        books = [
            book("e1", 1010, score=0.0015, imbalance=0.6, sell=True),
            book("e1", 1060, score=-0.0010, imbalance=-0.4),
            book("e1", 1080, score=-0.0030, imbalance=-0.8),
        ]
        rows, diag = pathmod.build_path_rows(
            maker, books, markout_horizon="250ms", placement_actions={"JOIN"},
            cancel_latency_ms=25.0,
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["causal_cutoff_ms"], 1075)
        self.assertEqual(row["path_last_receive_ms"], 1060)
        self.assertEqual(row["prefill_features"]["path_observations"], 2.0)
        self.assertAlmostEqual(row["prefill_features"]["minimum_micro_score"], -0.0010)
        self.assertAlmostEqual(row["prefill_features"]["micro_score_deterioration"], 0.0030)
        self.assertEqual(row["prefill_features"]["last_sell_age_cut_ms"], 65.0)
        self.assertEqual(diag["fills_with_prefill_path"], 1)

    def test_no_prefill_observation_before_latency_cut_fails_closed(self) -> None:
        maker = [maker_order("o1", "e1", 1000), fill("o1", "f1", 1020), mark("o1", "f1", 1300, -0.01)]
        books = [book("e1", 1010, score=0.0, imbalance=0.0)]
        rows, diag = pathmod.build_path_rows(
            maker, books, markout_horizon="250ms", placement_actions={"JOIN"},
            cancel_latency_ms=25.0,
        )
        self.assertEqual(rows, [])
        self.assertEqual(diag["fills_missing_causal_prefill_path"], 1)

    def test_synthetic_deterioration_predicts_adverse_fill_out_of_sample(self) -> None:
        rows = []
        for cluster in range(40):
            for adverse in (0, 1):
                features = {name: 0.0 for name in pathmod.PATH_FEATURES}
                features["micro_score_available"] = 1.0
                features["micro_score_deterioration"] = 0.005 if adverse else 0.0001
                features["imbalance_deterioration"] = 1.5 if adverse else 0.1
                features["maximum_sell_print_rate"] = 2.0 if adverse else 0.2
                rows.append({
                    "event_cluster": f"e{cluster:03d}",
                    "order_ts_ms": 10_000 + cluster * 100 + adverse,
                    "filled_shares": 2.0,
                    "markout_per_share": -0.02 if adverse else 0.02,
                    "adverse": adverse,
                    "cancel_latency_ms": 25.0,
                    "prefill_features": features,
                })
        train, validation, test, _ = base.chronological_cluster_split(rows)
        prep = pathmod.fit_preprocessor(train)
        beta = pathmod.fit_logistic(train, prep, ridge=1.0, iterations=2000)
        self.assertGreater(pathmod.auc(test, prep, beta), 0.95)
        threshold, _ = pathmod.choose_threshold(validation, prep, beta, 0.25)
        selected = pathmod.selected_metrics(test, prep, beta, threshold)
        self.assertGreater(selected["coverage"], 0.20)
        self.assertLess(selected["adverse_rate"], 0.25)
        self.assertGreater(selected["share_weighted_markout_per_share"], 0.0)

    def test_fit_fails_closed_below_cluster_gate(self) -> None:
        features = {name: 0.0 for name in pathmod.PATH_FEATURES}
        row = {
            "event_cluster": "e1", "order_ts_ms": 1000, "filled_shares": 1.0,
            "markout_per_share": -0.01, "adverse": 1, "cancel_latency_ms": 25.0,
            "prefill_features": features,
        }
        result = pathmod.fit_one_latency(
            [row], minimum_clusters=3, minimum_coverage=0.2, ridge=1.0,
            iterations=100, bootstrap_samples=0, seed=1,
        )
        self.assertEqual(result["state"], "INSUFFICIENT_PREFILL_PATH_EVIDENCE")
        self.assertEqual(result["clusters"], 1)


if __name__ == "__main__":
    unittest.main()
