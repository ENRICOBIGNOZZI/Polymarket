from __future__ import annotations

import argparse
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


def research_args() -> argparse.Namespace:
    return argparse.Namespace(
        min_recent_trades=2,
        min_sell_prints=2,
        max_event_age_seconds=60,
        min_fill_probability=0.02,
        max_sell_toxicity=0.80,
        improve_ticks=1,
        min_edge=0.00005,
    )


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
        seeded._SEED_RESOLUTION_MODE = {}

    def test_gamma_condition_query_repeats_condition_ids(self):
        query = urllib.parse.parse_qs(seeded._condition_market_query(["0x" + "a" * 64, "0x" + "b" * 64]))
        self.assertEqual(query["condition_ids"], ["0x" + "a" * 64, "0x" + "b" * 64])
        self.assertEqual(query["active"], ["true"])
        self.assertEqual(query["closed"], ["false"])

    def test_gamma_token_query_repeats_clob_token_ids(self):
        query = urllib.parse.parse_qs(seeded._token_market_query(["token-a", "token-b"]))
        self.assertEqual(query["clob_token_ids"], ["token-a", "token-b"])
        self.assertEqual(query["active"], ["true"])
        self.assertEqual(query["closed"], ["false"])
        self.assertEqual(query["limit"], ["100"])

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

    def test_malformed_data_condition_without_slug_is_resolved_by_clob_token(self):
        prior = [market("m1", "c1", 100), market("m2", "c2", 90)]
        raw_condition = "0x" + "3" * 62
        canonical = "0x" + "a" * 64
        global_rows = [
            {"conditionId": raw_condition, "asset": "yes-hot", "side": "SELL",
             "timestamp": 995, "price": 0.40, "size": 12.0, "transactionHash": "h"},
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
        self.assertEqual(seeded._SEED_DIAGNOSTICS["seed_global_asset_count"], 1)
        self.assertEqual(seeded._SEED_DIAGNOSTICS["seed_noncanonical_raw_condition_count"], 1)
        self.assertEqual(seeded._SEED_DIAGNOSTICS["seed_gamma_market_count"], 1)
        self.assertEqual(seeded._SEED_DIAGNOSTICS["seed_resolved_by_token_count"], 1)
        self.assertEqual(seeded._SEED_DIAGNOSTICS["seed_added_market_count"], 1)
        self.assertEqual(seeded._CANONICAL_CONDITION_BY_RAW[raw_condition], canonical)
        self.assertEqual(seeded._TOKEN_TO_CANONICAL_CONDITION["yes-hot"], canonical)
        self.assertEqual(seeded._SEED_RESOLUTION_MODE[raw_condition], "clob_token_ids")
        self.assertTrue(seeded._SEED_DIAGNOSTICS["authorized_market_cap_respected"])
        gamma_url = request.call_args_list[1].args[0]
        self.assertIn("clob_token_ids=yes-hot", gamma_url)
        self.assertNotIn("condition_ids=", gamma_url)

    def test_slug_fallback_remains_available_without_asset(self):
        raw_condition = "0x" + "4" * 62
        canonical = "0x" + "b" * 64
        seeded._SEED_SLUGS = {raw_condition: "slug-hot"}
        seeded._SEED_ASSETS.clear()
        with patch.object(seeded.core, "request_json", return_value=([gamma_row("hot", canonical, 1)], 1000)) as request:
            markets, errors = seeded._markets_for_conditions([raw_condition], 2.0)
        self.assertEqual(errors, [])
        self.assertEqual([m.condition_id for m in markets], [canonical])
        self.assertEqual(seeded._SEED_RESOLUTION_MODE[raw_condition], "slug_token")
        self.assertIn("slug=slug-hot", request.call_args.args[0])

    def test_global_token_flow_is_rekeyed_to_canonical_condition(self):
        canonical = "0x" + "a" * 64
        seeded._TOKEN_TO_CANONICAL_CONDITION = {"yes-hot": canonical}
        rows = [
            {"conditionId": "0x" + "3" * 62, "asset": "yes-hot", "side": "SELL",
             "timestamp": 995, "price": 0.40, "size": 12.0, "transactionHash": "h"},
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
                "seed_global_recent_rows": 4,
                "seed_global_condition_count": 2,
                "seed_global_asset_count": 2,
                "seed_gamma_market_count": 0,
                "seed_resolved_by_token_count": 0,
                "seed_errors": ["seed_gamma_token_batch=0:HTTPError:500"],
            }
        }
        self.assertFalse(seeded.seeded_activity_data_healthy(result))

    def test_unmapped_recent_assets_fail_closed_even_without_transport_error(self):
        result = {
            "universe": {
                "discovered_markets": 1000,
                "active_markets_evaluated": 1,
                "flow_errors": [],
                "global_sanity_checked": False,
                "seed_global_recent_rows": 4,
                "seed_global_condition_count": 2,
                "seed_global_asset_count": 2,
                "seed_gamma_market_count": 0,
                "seed_resolved_by_token_count": 0,
                "seed_errors": [],
            }
        }
        self.assertFalse(seeded.seeded_activity_data_healthy(result))

    def test_seed_diagnostics_do_not_invalidate_healthy_nonzero_activity_without_seed_rows(self):
        result = {
            "universe": {
                "discovered_markets": 1000,
                "active_markets_evaluated": 5,
                "flow_errors": [],
                "global_sanity_checked": False,
                "seed_global_recent_rows": 0,
                "seed_global_condition_count": 0,
                "seed_global_asset_count": 0,
                "seed_gamma_market_count": 0,
                "seed_resolved_by_token_count": 0,
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

    def test_sparse_one_print_fill_evidence_is_not_hard_rejected_as_certain_toxicity(self):
        a = research_args()
        seeded.apply_zero_fill_research_profile(a)
        sparse_sell = core.Flow(
            trade_count=1,
            buy_volume=0.0,
            sell_volume=12.72,
            compatible_sell_volume=12.72,
            compatible_sell_prints=1,
            last_event_age=73,
            signed_imbalance=-1.0,
        )
        self.assertGreater(core.fill_probability_proxy(sparse_sell, 57.3, 14.325), 0.005)
        self.assertTrue(seeded.activity_eligible_reliability_gated_toxicity(sparse_sell, 57.3, 14.325, a))
        self.assertEqual(a.improve_ticks, 0)

    def test_recurrent_sell_toxicity_still_fails_closed(self):
        a = research_args()
        seeded.apply_zero_fill_research_profile(a)
        recurrent_toxic = core.Flow(
            trade_count=2,
            buy_volume=0.0,
            sell_volume=100.0,
            compatible_sell_volume=100.0,
            compatible_sell_prints=2,
            last_event_age=1,
            signed_imbalance=-1.0,
        )
        self.assertFalse(seeded.activity_eligible_reliability_gated_toxicity(recurrent_toxic, 10.0, 10.0, a))

    def test_sparse_flow_cannot_authorize_inside_spread_improvement(self):
        a = research_args()
        seeded.apply_zero_fill_research_profile(a)
        a.improve_ticks = 1
        sparse_sell = core.Flow(1, 0.0, 100.0, 100.0, 1, 1, -1.0)
        self.assertFalse(seeded.activity_eligible_reliability_gated_toxicity(sparse_sell, 10.0, 10.0, a))


if __name__ == "__main__":
    unittest.main()