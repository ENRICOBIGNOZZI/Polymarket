#!/usr/bin/env python3
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


universe = load_module("all_market_universe", ROOT / "scripts" / "all_market_universe.py")
book = load_module("global_opportunity_book", ROOT / "scripts" / "build_global_opportunity_book.py")
account = load_module("account_readonly", ROOT / "scripts" / "polymarket_account_readonly.py")


class AllMarketEngineTests(unittest.TestCase):
    def test_universe_tiers_and_tokens(self):
        market = {
            "id": "11",
            "conditionId": "0xabc",
            "slug": "sample",
            "question": "Sample?",
            "liquidityNum": 150,
            "volume24hr": 25,
            "active": True,
            "closed": False,
            "enableOrderBook": True,
            "acceptingOrders": True,
            "clobTokenIds": json.dumps(["yes-token", "no-token"]),
            "outcomes": json.dumps(["Yes", "No"]),
            "events": [{"id": "event-1"}],
        }
        row = universe.normalized_market(market, 20.0, 100.0)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["tier"], 2)
        self.assertEqual(row["yes_token"], "yes-token")
        self.assertEqual(row["no_token"], "no-token")
        self.assertEqual(row["event_id"], "event-1")

    def test_universe_rejects_non_tradable(self):
        market = {
            "id": "12",
            "conditionId": "0xdef",
            "active": True,
            "closed": True,
            "enableOrderBook": True,
            "acceptingOrders": True,
            "clobTokenIds": ["a", "b"],
            "outcomes": ["Yes", "No"],
        }
        self.assertIsNone(universe.normalized_market(market, 20.0, 100.0))

    def test_l2_signature_is_get_only_and_exact(self):
        secret_bytes = b"secret-material"
        secret = base64.urlsafe_b64encode(secret_bytes).decode("ascii")
        timestamp = "1700000000"
        path = "/data/orders"
        expected = base64.urlsafe_b64encode(
            hmac.new(secret_bytes, (timestamp + "GET" + path).encode(), hashlib.sha256).digest()
        ).decode("ascii")
        self.assertEqual(account.l2_signature(secret, timestamp, "GET", path), expected)
        with self.assertRaises(ValueError):
            account.l2_signature(secret, timestamp, "POST", "/order")

    def test_global_book_prioritizes_executable_positive_edge(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "fast").mkdir(parents=True)
            fast_path = root / "fast" / "fast_arb_latest.csv"
            with fast_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "kind", "id", "event_id", "executable", "hard_arbitrage",
                        "raw_edge_per_share", "net_edge_per_share", "capital_required",
                        "expected_profit", "legs",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "kind": "BINARY_COMPLETE_SET",
                        "id": "fast-1",
                        "event_id": "event",
                        "executable": "1",
                        "hard_arbitrage": "1",
                        "raw_edge_per_share": "0.02",
                        "net_edge_per_share": "0.01",
                        "capital_required": "50",
                        "expected_profit": "0.5",
                        "legs": "a|b",
                    }
                )
            b1_path = root / "stat_arb_pairs.csv"
            with b1_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "maker_entry_net_edge", "raw_expected_edge", "executable_notional",
                        "y_market", "x_market", "y_side", "x_side", "y_weight", "x_weight",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "maker_entry_net_edge": "0.005",
                        "raw_expected_edge": "0.008",
                        "executable_notional": "100",
                        "y_market": "1",
                        "x_market": "2",
                        "y_side": "YES",
                        "x_side": "NO",
                        "y_weight": "1",
                        "x_weight": "1",
                    }
                )
            candidates = list(book.fast_rows(fast_path)) + list(book.b1_rows(b1_path, 250.0))
            candidates = book.deduplicate(candidates)
            candidates.sort(key=lambda row: (-int(row["eligible"]), -int(row["hard_arbitrage"]), -float(row["score"])))
            self.assertEqual(candidates[0]["source_id"], "fast-1")
            self.assertTrue(all(float(row["raw_edge"]) > 0 for row in candidates))

    def test_terminal_candidates_are_fresh_and_use_universal_experts(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "signals.csv"
            fields = [
                "timestamp", "market_id", "slug", "side", "mid", "exec_price", "fair_side",
                "fair_yes", "uncertainty", "fee_per_share", "slippage_per_share", "gross_edge",
                "cost_adjusted_edge", "net_edge", "score", "desired_notional", "experts",
            ]
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow({
                    "timestamp": "900", "market_id": "old", "side": "YES", "gross_edge": "0.2",
                    "net_edge": "0.1", "desired_notional": "100", "experts": "external:0.8:1",
                })
                writer.writerow({
                    "timestamp": "1990", "market_id": "fresh", "side": "NO", "gross_edge": "0.02",
                    "net_edge": "0.01", "desired_notional": "50",
                    "experts": "micro:0.4:0.2|graph:0.45:0.9|semantic:0.43:0.3|external:0.42:0.7",
                })
                writer.writerow({
                    "timestamp": "1995", "market_id": "fresh", "side": "NO", "gross_edge": "0.03",
                    "net_edge": "0.015", "desired_notional": "60",
                    "experts": "micro:0.4:0.2|graph:0.46:0.9|semantic:0.44:0.3|external:0.43:0.7",
                })
            rows = list(book.terminal_rows(path, 250.0, now_ts=2000, max_age_seconds=600))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["market_id"], "fresh")
            self.assertEqual(rows[0]["strategy"], "TERMINAL:external+graph+semantic+micro")
            self.assertEqual(rows[0]["eligible"], 1)
            self.assertAlmostEqual(float(rows[0]["net_edge"]), 0.015)

    def test_cpp_discovery_contract_is_keyset_and_unbounded_capable(self):
        source = (ROOT / "src" / "api.cpp").read_text(encoding="utf-8")
        self.assertIn('/markets/keyset?', source)
        self.assertIn('const bool unbounded = limit == 0;', source)
        self.assertNotIn('std::min<std::size_t>(limit, 2000)', source)
        self.assertNotIn('&offset=', source)
        self.assertNotIn('order=liquidity_num', source)

    def test_policy_preserves_paper_only_boundary(self):
        policy = json.loads((ROOT / "config" / "all_market_engine.json").read_text(encoding="utf-8"))
        self.assertEqual(policy["target_candidate_book"], 1000)
        self.assertFalse(policy["safety"]["authenticated_execution"])
        self.assertFalse(policy["safety"]["real_order_submission"])
        self.assertTrue(policy["account_adapter"]["read_only"])


if __name__ == "__main__":
    unittest.main()
