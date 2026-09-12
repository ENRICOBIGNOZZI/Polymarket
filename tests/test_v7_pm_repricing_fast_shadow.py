import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_maker_execution_horse_race import learned_events
from v7_pm_repricing_fast_shadow import causal_origin_evidence
from v7_pm_repricing_labeler import valid_inference


class FakeBook:
    def __init__(self, yes, no):
        self.rows = {"yes": yes, "no": no}

    def asof(self, market, token, timestamp_ms):
        return self.rows.get(token)


def cut(token, receive_ms, bid, ask, tick=0.01):
    return {
        "token_id": token,
        "receive_wall_ms": receive_ms,
        "best_bid": bid,
        "best_ask": ask,
        "tick_size": tick,
        "valid": True,
        "lineage_continuous": True,
    }


class FastShadowTest(unittest.TestCase):
    def origin(self):
        return {
            "origin_id": "o1",
            "market_id": "m1",
            "yes_token": "yes",
            "no_token": "no",
            "origin_observed_wall_ns": 1_000_000_000_000,
            "origin_pm_yes": 0.50,
            "origin_pm_snapshot_id": "s1",
        }

    def test_origin_book_cut_is_causal_and_fresh(self):
        origin = self.origin()
        evidence = causal_origin_evidence(
            FakeBook(
                cut("yes", 999_990, 0.49, 0.51),
                cut("no", 999_995, 0.49, 0.51),
            ),
            origin,
            max_book_age_ms=100,
        )
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["origin_pm_yes"], 0.50)
        self.assertEqual(len(evidence["origin_book_cuts"]), 2)

    def test_stale_origin_book_is_rejected_before_scoring(self):
        origin = self.origin()
        evidence = causal_origin_evidence(
            FakeBook(
                cut("yes", 999_800, 0.49, 0.51),
                cut("no", 999_800, 0.49, 0.51),
            ),
            origin,
            max_book_age_ms=100,
        )
        self.assertIsNone(evidence)

    def test_late_raw_veto_cannot_enter_horse_race(self):
        row = {
            "schema": "polymarket_v7_pm_repricing_shadow_v1",
            "paper_only": True,
            "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
            "family": "PM_PLUS_EXTERNAL",
            "horizon_ms": 250,
            "threshold_ticks": 1.0,
            "scored_wall_ns": 1_000_000,
            "market_id": "m1",
            "origin_id": "o1",
            "raw_would_veto_yes_buy": True,
            "would_veto_yes_buy": False,
            "would_veto_no_buy": False,
        }
        self.assertEqual(
            learned_events([row], family="PM_PLUS_EXTERNAL", horizon_ms=250, threshold=1.0),
            [],
        )

    def test_labeler_accepts_only_origin_only_safe_inference(self):
        row = {
            "schema": "polymarket_v7_pm_repricing_shadow_v1",
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
            "runtime_model_sha": "a" * 40,
            "inference_phase": "ORIGIN_ONLY_NO_FUTURE_LABEL",
            "horizon_ms": 250,
            "origin_id": "o1",
            "market_id": "m1",
            "yes_token": "yes",
            "no_token": "no",
            "origin_observed_wall_ns": 1,
        }
        self.assertTrue(valid_inference(row, "a" * 40, 250))
        row["inference_phase"] = "FUTURE_LABEL_ATTACHED"
        self.assertFalse(valid_inference(row, "a" * 40, 250))


if __name__ == "__main__":
    unittest.main()
