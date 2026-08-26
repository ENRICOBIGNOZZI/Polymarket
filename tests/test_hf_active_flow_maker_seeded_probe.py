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
        seeded._SEED_SLUGS = {}
        seeded._SEED_ASSETS.clear()
        seeded._TOKEN_TO_CANONICAL_CONDITION = {}
        seeded._CANONICAL_CONDITION_BY_RAW = {}

    def test_gamma_condition_query_repeats_condition_ids(self):
        query = urllib.parse.parse_qs(seeded._condition_market_query(["0x" + "a" * 64, "0x" + "b" * 64]))
        self.assertEqual(query["condition_ids"], ["0x" + "a" * 64, "0x" + "b" * 64])
        self.assertEqual(query["active"], ["true"])
        self.assertEqual(query["closed"], ["false"])

    def test_gamma_slug_query_is_bounded_and_active_only(self):
        query = urllib.parse.parse_qs(seeded._slug_market_query("slug-hot"))
        self.assertEqual(query["slug"], ["slug-hot"])
        self.assertEqual(query["active"], ["true"])
        self.assertEqual(query["closed"], ["false"])
        self.assertEqual(query["limit"], ["10"])

    def test_merge_seeded_universe_injects_recent_market_and_respects_cap(self):
        prior = [market("m1", "c1", 100), market("m2", "c2", 90), market("m3", "c3", 80)]
        seeds = [market("hot", "c-hot", 1), market("m2", "c2", 90)]
        merged = seeded.merge_seeded_universe(prior, seeds, 3)
        self.assertEqual([m.condition_id for m in merged], ["c-hot", "c2", "c1"])
        self.assertEqual(len(merged), 3)

    def test_malformed_data_condition_is_resolved_by_slug_and_token_identity(self):
        prior = [market("m1", "c1", 100), market("m2", "c2", 90)]
        raw_condition = "0x" + "3" * 62
        canonical = "0x" + "a" * 64
        global_rows = [
            {"conditionId": raw_condition, "asset": "yes-hot", "side": "SELL",
             "timestamp": 995, "price": 0.40, "size": 12.0, "transactionHash": "h",
             "slug": "slug-hot"},
        ]
        with patch.object(seeded, "_ORIGINAL_DISCOVER", return_value=prior), \
             patch.object(seeded.core, "now_s", return_value=1000), \
             patch.object(seeded.core, "request_json") as request:
            request.side_effect = [
                (global_rows, 1_000_100),
                ([gamma_row("hot", canonical, 1)], 1_000_200),
            ]
            merged = seeded.discover_markets_seeded(2, 2.0)
        self.assertEqual([m.condition_id for m in merged], [canonical, "c1"])
        self.assertEqual(seeded._SEED_DIAGNOSTICS["seed_global_condition_count"], 1)
        self.assertEqual(seeded._SEED_DIAGNOSTICS["seed_noncanonical_raw_condition_count"], 1)
        self.assertEqual(seeded._SEED_DIAGNOSTICS["seed_gamma_market_count"], 1)
        self.assertEqual(seeded._SEED_DIAGNOSTICS["seed_added_market_count"], 1)
        self.assertEqual(seeded._CANONICAL_CONDITION_BY_RAW[raw_condition], canonical)
        self.assertEqual(seeded._TOKEN_TO_CANONICAL_CONDITION["yes-hot"], canonical)
        self.assertTrue(seeded._SEED_DIAGNOSTICS["authorized_market_cap_respected"])
        gamma_url = request.call_args_list[1].args[0]
        self.assertIn("slug=slug-hot", gamma_url)

    def test_global_token_flow_is_rekeyed_to_canonical_condition(self):
        canonical = "0x" + "a" * 64
        seeded._TOKEN_TO_CANONICAL_CONDITION = {"yes-hot": canonical}
        rows = [
            {"conditionId": "0x" + "3" * 62, "asset": "yes-hot", "side": "SELL",
             "timestamp": 995, "price": 0.40, "size": 12.0, "transactionHash": "h",
             "slug": "slug-hot"},
        ]
        with patch.object(seeded.core, "request_json", return_value=(rows, 1_000_300)):
            flows: dict[str, list[core.Trade]] = {}
            received_ms, errors = seeded._merge_global_token_flows(flows, 900, 1000)
        self.assertEqual(errors, [])
        self.assertEqual(received_ms, 1_000_300)
        self.assertEqual(len(flows[canonical]), 1)
        self.assertEqual(flows[canonical][0].token_id, "yes-hot")

    def test_seed_mapping_failure_fails_closed_if_no_active_market(self):
        result = {
            "universe": {
                "discovered_markets": 1000,
                "active_markets_evaluated": 0,
                "flow_errors": [],
                "global_sanity_checked": False,
                "seed_global_condition_count": 2,
                "seed_gamma_market_count": 0,
                "seed_errors": ["seed_gamma_slug=x:HTTPError:500"],
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
