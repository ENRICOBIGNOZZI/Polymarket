from __future__ import annotations

import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str((Path(__file__).resolve().parents[1] / "scripts")))

import hf_active_flow_maker_core as core  # noqa: E402
import hf_active_flow_maker_seeded_probe as seeded  # noqa: E402


def market(mid: str, condition: str, volume: float = 10.0) -> core.Market:
    return core.Market(
        mid,
        condition,
        "event-" + mid,
        "slug-" + mid,
        "yes-" + mid,
        "no-" + mid,
        100.0,
        volume,
        core.Fee(0.0, 1.0, True, "test"),
    )


def gamma_row(mid: str, condition: str, volume: float = 10.0) -> dict[str, object]:
    return {
        "id": mid,
        "conditionId": condition,
        "active": True,
        "closed": False,
        "enableOrderBook": True,
        "acceptingOrders": True,
        "liquidityNum": 100.0,
        "volume24hr": volume,
        "clobTokenIds": ["yes-" + mid, "no-" + mid],
        "outcomes": ["Yes", "No"],
        "events": [{"id": "event-" + mid}],
        "slug": "slug-" + mid,
        "feesEnabled": False,
    }


class ActiveFlowMakerSeededProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        seeded._SEED_DIAGNOSTICS = {}
        seeded._FLOW_CONDITIONS = set()
        seeded._PRIOR_CONDITIONS = set()
        seeded._SEED_CONDITIONS = set()
        seeded._SEED_ADDED_CONDITIONS = set()

    def test_gamma_condition_query_repeats_condition_ids(self):
        query = urllib.parse.parse_qs(seeded._condition_market_query(["0xa", "0xb"]))
        self.assertEqual(query["condition_ids"], ["0xa", "0xb"])
        self.assertEqual(query["active"], ["true"])
        self.assertEqual(query["closed"], ["false"])

    def test_merge_seeded_universe_injects_recent_market_and_respects_cap(self):
        prior = [market("m1", "c1", 100), market("m2", "c2", 90), market("m3", "c3", 80)]
        seeds = [market("hot", "c-hot", 1), market("m2", "c2", 90)]
        merged = seeded.merge_seeded_universe(prior, seeds, 3)
        self.assertEqual([m.condition_id for m in merged], ["c-hot", "c2", "c1"])
        self.assertEqual(len(merged), 3)

    def test_recent_global_trade_condition_is_mapped_into_capped_universe(self):
        prior = [market("m1", "c1", 100), market("m2", "c2", 90)]
        global_rows = [
            {"conditionId": "c-hot", "asset": "yes-hot", "side": "SELL",
             "timestamp": 995, "price": 0.40, "size": 12.0, "transactionHash": "h"},
        ]
        with patch.object(seeded, "_ORIGINAL_DISCOVER", return_value=prior), \
             patch.object(seeded.core, "now_s", return_value=1000), \
             patch.object(seeded.core, "request_json") as request:
            request.side_effect = [
                (global_rows, 1_000_100),
                ([gamma_row("hot", "c-hot", 1)], 1_000_200),
            ]
            merged = seeded.discover_markets_seeded(2, 2.0)
        self.assertEqual([m.condition_id for m in merged], ["c-hot", "c1"])
        self.assertEqual(seeded._SEED_DIAGNOSTICS["seed_global_condition_count"], 1)
        self.assertEqual(seeded._SEED_DIAGNOSTICS["seed_gamma_market_count"], 1)
        self.assertEqual(seeded._SEED_DIAGNOSTICS["seed_added_market_count"], 1)
        self.assertTrue(seeded._SEED_DIAGNOSTICS["authorized_market_cap_respected"])
        gamma_url = request.call_args_list[1].args[0]
        self.assertIn("condition_ids=c-hot", gamma_url)

    def test_seed_mapping_failure_fails_closed_if_no_active_market(self):
        result = {
            "universe": {
                "discovered_markets": 1000,
                "active_markets_evaluated": 0,
                "flow_errors": [],
                "global_sanity_checked": False,
                "seed_global_condition_count": 2,
                "seed_gamma_market_count": 0,
                "seed_errors": ["seed_gamma_batch=0:HTTPError:500"],
            }
        }
        self.assertFalse(seeded.seeded_activity_data_healthy(result))

    def test_seed_diagnostics_do_not_invalidate_healthy_nonzero_activity(self):
        result = {
            "universe": {
                "discovered_markets": 1000,
                "active_markets_evaluated": 5,
                "flow_errors": [],
                "global_sanity_checked": False,
                "seed_global_condition_count": 0,
                "seed_gamma_market_count": 0,
                "seed_errors": ["seed_global:TimeoutError"],
            }
        }
        self.assertTrue(seeded.seeded_activity_data_healthy(result))

    def test_global_seed_query_is_strictly_bounded_to_recent_window(self):
        query = urllib.parse.parse_qs(seeded._global_seed_query(100, 200))
        self.assertEqual(query["start"], ["100"])
        self.assertEqual(query["end"], ["200"])
        self.assertEqual(query["limit"], ["1000"])
        self.assertEqual(query["takerOnly"], ["true"])


if __name__ == "__main__":
    unittest.main()
