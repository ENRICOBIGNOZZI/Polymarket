from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "v7_maker_decision_time_toxicity",
    SCRIPTS / "v7_maker_decision_time_toxicity.py",
)
assert SPEC and SPEC.loader
dt = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dt
SPEC.loader.exec_module(dt)
import v7_maker_fill_conditioned_toxicity as base

SHA = "a" * 40
OTHER_SHA = "b" * 40


def receipt() -> dict:
    return {
        "owner": "V7_GLOBAL_PORTFOLIO_COORDINATOR",
        "action": "MAKE",
        "paper_only": True,
        "paper_exploration_authorized": True,
        "authenticated_execution": False,
        "real_order_submission": False,
    }


def order(order_id: str, cluster: str, ts: int, *, score: float = 0.002, imbalance: float = 0.6) -> dict:
    return {
        "event_type": "ORDER_SUBMITTED",
        "model_sha": SHA,
        "paper_only": True,
        "authenticated_execution": False,
        "order_id": order_id,
        "event_id": cluster,
        "market_id": "m-" + cluster,
        "token_id": "yes-" + cluster,
        "side": "BUY",
        "recorded_ts_ms": ts,
        "metadata": {
            "component": "professional_maker",
            "execution_semantics_version": base.SEMANTICS,
            "placement_action": "JOIN",
            "paper_bootstrap_probe": False,
            "paper_exploration": True,
            "economic_authority": "PAPER_EXPLORATION",
            "counterfactual": False,
            "excluded_from_portfolio_equity": False,
            "coordinator_receipt": receipt(),
            "placement_features": {
                "microstructure_shadow_delta_250ms": score,
                "imbalance": imbalance,
                "ofi": 0.2,
                "cancel_intensity": 0.1,
                "trade_intensity": 0.2,
                "aggressive_sell_prints_per_second": 0.3,
                "spread_ticks": 1.0,
                "distance_from_touch_ticks": 0.0,
            },
            "execution_alpha": {
                "features": {"queue_ahead": 20.0},
                "fill_probability": {"point": 0.2},
            },
        },
    }


def fill(order_id: str, cluster: str, fill_id: str, ts: int) -> dict:
    return {
        "event_type": "FILL",
        "model_sha": SHA,
        "paper_only": True,
        "authenticated_execution": False,
        "order_id": order_id,
        "fill_id": fill_id,
        "event_id": cluster,
        "market_id": "m-" + cluster,
        "token_id": "yes-" + cluster,
        "recorded_ts_ms": ts,
        "filled_size": 2.0,
    }


def mark(order_id: str, fill_id: str, ts: int, value: float) -> dict:
    return {
        "event_type": "MARKOUT",
        "model_sha": SHA,
        "paper_only": True,
        "authenticated_execution": False,
        "order_id": order_id,
        "fill_id": fill_id,
        "recorded_ts_ms": ts,
        "markouts": {"250ms": value},
    }


def terminal(order_id: str, ts: int, state: str = "CANCELLED") -> dict:
    return {
        "event_type": "ORDER_STATE",
        "model_sha": SHA,
        "paper_only": True,
        "authenticated_execution": False,
        "order_id": order_id,
        "recorded_ts_ms": ts,
        "order_state": state,
    }


def book(cluster: str, ts: int, *, score: float, imbalance: float, sha: str = SHA, sell: bool = False) -> dict:
    return {
        "schema": "polymarket_v7_causal_book_observation_v1",
        "model_sha": sha,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
        "valid": True,
        "features_valid": True,
        "lineage_continuous": True,
        "observer_session_id": "s1",
        "connection_epoch": 1,
        "observer_sequence": ts,
        "market_id": "m-" + cluster,
        "token_id": "yes-" + cluster,
        "receive_wall_ms": ts,
        "placement_features": {
            "microstructure_shadow_delta_250ms": score,
            "imbalance": imbalance,
            "ofi": imbalance * 0.5,
            "cancel_intensity": max(0.0, -imbalance),
            "trade_intensity": abs(imbalance),
            "aggressive_sell_prints_per_second": 1.0 if sell else 0.1,
            "spread_ticks": 1.0,
            "distance_from_touch_ticks": 0.0,
        },
        "public_trade": {"aggressor_side": "SELL", "size": 1.0} if sell else None,
    }


class DecisionTimeToxicityTests(unittest.TestCase):
    def test_decision_clock_uses_observed_book_times_not_fill_relative_cut(self) -> None:
        maker = [
            order("o1", "e1", 1000),
            fill("o1", "e1", "f1", 1100),
            mark("o1", "f1", 1400, -0.02),
        ]
        books = [
            book("e1", 1010, score=0.0015, imbalance=0.5, sell=True),
            book("e1", 1060, score=-0.0010, imbalance=-0.3),
            book("e1", 1080, score=-0.0030, imbalance=-0.8),
        ]
        rows, diag = dt.build_decision_rows(
            maker, books,
            markout_horizon="250ms",
            placement_actions={"JOIN"},
            cancel_latency_ms=25.0,
            fill_hazard_window_ms=500,
            minimum_decision_spacing_ms=0,
        )
        self.assertEqual([row["decision_ts_ms"] for row in rows], [1010, 1060, 1080])
        self.assertEqual(rows[1]["avoidable_adverse_fill"], 1)
        self.assertEqual(rows[1]["unavoidable_fill_before_cancel_effective"], 0)
        self.assertEqual(rows[2]["avoidable_adverse_fill"], 0)
        self.assertEqual(rows[2]["unavoidable_fill_before_cancel_effective"], 1)
        self.assertEqual(diag["decision_rows_with_unavoidable_fill"], 1)

    def test_noncanonical_coordinator_receipt_excludes_order_entirely(self) -> None:
        bad = order("o1", "e1", 1000)
        bad["metadata"]["coordinator_receipt"]["action"] = "CANCEL"
        rows, diag = dt.build_decision_rows(
            [bad, terminal("o1", 1700)],
            [book("e1", 1050, score=0.001, imbalance=0.2)],
            markout_horizon="250ms",
            placement_actions={"JOIN"},
            cancel_latency_ms=25.0,
            fill_hazard_window_ms=500,
            minimum_decision_spacing_ms=0,
        )
        self.assertEqual(rows, [])
        self.assertEqual(diag["candidate_orders"], 0)

    def test_wrong_sha_book_rows_never_enter_decision_dataset(self) -> None:
        maker = [order("o1", "e1", 1000), terminal("o1", 1500)]
        books = [
            book("e1", 1010, score=-0.9, imbalance=-0.9, sha=OTHER_SHA),
            book("e1", 1020, score=0.001, imbalance=0.2, sha=SHA),
        ]
        rows, _ = dt.build_decision_rows(
            maker, books,
            markout_horizon="250ms",
            placement_actions={"JOIN"},
            cancel_latency_ms=25.0,
            fill_hazard_window_ms=500,
            minimum_decision_spacing_ms=0,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["decision_ts_ms"], 1020)
        self.assertAlmostEqual(rows[0]["features"]["micro_score"], 0.001)

    def test_nonpaper_fill_cannot_label_canonical_order(self) -> None:
        fake_fill = fill("o1", "e1", "f1", 1100)
        fake_fill["paper_only"] = False
        rows, _ = dt.build_decision_rows(
            [order("o1", "e1", 1000), fake_fill, terminal("o1", 1700)],
            [book("e1", 1050, score=0.001, imbalance=0.2)],
            markout_horizon="250ms", placement_actions={"JOIN"},
            cancel_latency_ms=25.0, fill_hazard_window_ms=500,
            minimum_decision_spacing_ms=0,
        )
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["label_complete"])
        self.assertEqual(rows[0]["first_fill_in_horizon"], 0)

    def test_future_book_update_cannot_change_earlier_feature_cut(self) -> None:
        maker = [order("o1", "e1", 1000), terminal("o1", 1500)]
        first = book("e1", 1050, score=0.002, imbalance=0.4)
        later = book("e1", 1090, score=-0.010, imbalance=-0.9)
        rows_a, _ = dt.build_decision_rows(
            maker, [first],
            markout_horizon="250ms", placement_actions={"JOIN"},
            cancel_latency_ms=25.0, fill_hazard_window_ms=500,
            minimum_decision_spacing_ms=0,
        )
        rows_b, _ = dt.build_decision_rows(
            maker, [first, later],
            markout_horizon="250ms", placement_actions={"JOIN"},
            cancel_latency_ms=25.0, fill_hazard_window_ms=500,
            minimum_decision_spacing_ms=0,
        )
        self.assertEqual(rows_a[0]["features"], rows_b[0]["features"])

    def test_historical_cancel_before_keep_horizon_is_censored_not_negative(self) -> None:
        maker = [order("o1", "e1", 1000), terminal("o1", 1200, "CANCELLED")]
        rows, diag = dt.build_decision_rows(
            maker,
            [book("e1", 1050, score=0.001, imbalance=0.2)],
            markout_horizon="250ms", placement_actions={"JOIN"},
            cancel_latency_ms=25.0, fill_hazard_window_ms=500,
            minimum_decision_spacing_ms=0,
        )
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["label_complete"])
        self.assertEqual(rows[0]["censor_reason"], "HISTORICAL_TERMINAL_BEFORE_KEEP_HORIZON")
        self.assertEqual(diag["decision_rows_policy_censored"], 1)
        self.assertEqual(dt.supervised_rows(rows), [])

    def test_terminal_after_keep_horizon_is_valid_no_fill_negative(self) -> None:
        maker = [order("o1", "e1", 1000), terminal("o1", 1700, "CANCELLED")]
        rows, _ = dt.build_decision_rows(
            maker,
            [book("e1", 1050, score=0.001, imbalance=0.2)],
            markout_horizon="250ms", placement_actions={"JOIN"},
            cancel_latency_ms=25.0, fill_hazard_window_ms=500,
            minimum_decision_spacing_ms=0,
        )
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["label_complete"])
        self.assertEqual(rows[0]["first_fill_in_horizon"], 0)
        self.assertEqual(rows[0]["avoidable_adverse_fill"], 0)

    def test_open_order_without_terminal_proof_is_right_censored(self) -> None:
        rows, diag = dt.build_decision_rows(
            [order("o1", "e1", 1000)],
            [book("e1", 1050, score=0.001, imbalance=0.2)],
            markout_horizon="250ms", placement_actions={"JOIN"},
            cancel_latency_ms=25.0, fill_hazard_window_ms=500,
            minimum_decision_spacing_ms=0,
        )
        self.assertFalse(rows[0]["label_complete"])
        self.assertEqual(rows[0]["censor_reason"], "ORDER_LIFETIME_RIGHT_CENSORED")
        self.assertEqual(diag["decision_rows_right_censored"], 1)

    def test_synthetic_current_state_predicts_avoidable_adverse_fill_oos(self) -> None:
        rows = []
        for cluster in range(50):
            adverse = 1 if cluster % 2 else 0
            for step in range(2):
                features = {name: 0.0 for name in dt.FEATURES}
                features["micro_score_available"] = 1.0
                features["micro_score"] = -0.006 if adverse else 0.006
                features["micro_score_change_from_entry"] = -0.008 if adverse else 0.002
                features["imbalance"] = -0.8 if adverse else 0.8
                features["ofi"] = -0.7 if adverse else 0.7
                features["maximum_cancel_intensity_100ms"] = 1.5 if adverse else 0.1
                features["maximum_sell_print_rate_100ms"] = 2.0 if adverse else 0.2
                rows.append({
                    "event_cluster": f"e{cluster:03d}",
                    "decision_ts_ms": 10_000 + cluster * 100 + step,
                    "order_ts_ms": 9_000 + cluster * 100,
                    "cancel_latency_ms": 25.0,
                    "fill_hazard_window_ms": 500,
                    "label_complete": True,
                    "avoidable_adverse_fill": adverse,
                    "first_fill_in_horizon": 1,
                    "features": features,
                })
        train, validation, test, _ = base.chronological_cluster_split(
            [{**row, "order_ts_ms": row["decision_ts_ms"]} for row in rows]
        )
        prep = dt.fit_preprocessor(train)
        beta = dt.fit_logistic(
            train, prep, target="avoidable_adverse_fill", ridge=1.0, iterations=2000
        )
        self.assertGreater(dt.auc(test, prep, beta, "avoidable_adverse_fill"), 0.95)
        result = dt.fit_one_latency(rows, minimum_clusters=20, ridge=1.0, iterations=2000)
        self.assertEqual(result["state"], "FIT_COMPLETE_ZERO_AUTHORITY")
        self.assertFalse(result["policy_gate"]["cancel_authority"])
        self.assertIsNone(result["model"]["threshold"])

    def test_fit_fails_closed_below_cluster_gate(self) -> None:
        features = {name: 0.0 for name in dt.FEATURES}
        row = {
            "event_cluster": "e1",
            "decision_ts_ms": 1000,
            "order_ts_ms": 1000,
            "cancel_latency_ms": 25.0,
            "fill_hazard_window_ms": 500,
            "label_complete": True,
            "avoidable_adverse_fill": 1,
            "first_fill_in_horizon": 1,
            "features": features,
        }
        result = dt.fit_one_latency([row], minimum_clusters=3, ridge=1.0, iterations=50)
        self.assertEqual(result["state"], "INSUFFICIENT_DECISION_TIME_EVIDENCE")


if __name__ == "__main__":
    unittest.main()
