#!/usr/bin/env python3
from __future__ import annotations

import csv
import importlib.util
import json
import math
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class V6FrontierContracts(unittest.TestCase):
    def test_maker_recycles_zero_causal_flow_capital(self):
        maker = load_script("frontier_maker_v2", "scripts/v6_micro_maker_v2.py")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = {
                "orders": {
                    "m1": {
                        "market_id": "m1", "event_id": "e", "slug": "s", "side": "YES",
                        "token_id": "tok", "limit_price": 0.4, "remaining_shares": 10,
                        "queue_ahead": 50, "created_ts": int(time.time()) - 120,
                        "signal_edge": 0.01, "confidence": 0.5, "fill_probability": 0.1,
                        "expected_fill_edge": 0.001, "flow_rate": 0.0, "fee_source": "test",
                    }
                }
            }
            (root / "state.json").write_text(json.dumps(state), encoding="utf-8")
            tape = root / "tape.csv"
            with tape.open("w", newline="", encoding="utf-8") as h:
                csv.DictWriter(h, fieldnames=["timestamp", "asset_id", "side", "price", "size"]).writeheader()
            report = maker.recycle_dead_orders(root, tape, grace_seconds=20)
            self.assertEqual(report["cancelled"], 1)
            final = json.loads((root / "state.json").read_text())
            self.assertEqual(final["orders"], {})

    def test_micro_taker_v3_uses_spread_net_causal_target(self):
        micro = load_script("frontier_micro_v3", "scripts/v6_micro_taker_v3.py")
        samples = [
            {"ts": 100, "market_id": "m", "mid": 0.50, "spread": 0.01, "y": None},
            {"ts": 104, "market_id": "m", "mid": 0.52, "spread": 0.01, "y": None},
            {"ts": 106, "market_id": "m", "mid": 0.90, "spread": 0.01, "y": None},
        ]
        report = micro.label_executable_samples(
            samples, now=106, horizon_seconds=5, max_target_staleness_seconds=2
        )
        self.assertAlmostEqual(samples[0]["raw_mid_delta"], 0.02, places=12)
        self.assertAlmostEqual(samples[0]["spread_hurdle"], 0.01, places=12)
        self.assertAlmostEqual(samples[0]["y"], 0.01, places=12)
        self.assertEqual(samples[0]["target_observation_ts"], 104)
        self.assertEqual(report["target_kind"], "causal_spread_net_markout")
        self.assertEqual(len(micro.augment_vector([0.0] * 10, 0.5)), 14)

    def test_micro_taker_v3_robust_fit_is_finite(self):
        micro = load_script("frontier_micro_v3_fit", "scripts/v6_micro_taker_v3.py")
        now = int(time.time())
        rows = []
        for i in range(80):
            x = [1.0] + [((i + j) % 7 - 3) / 3.0 for j in range(13)]
            y = 0.002 * x[1] + (0.5 if i == 40 else 0.0)
            rows.append({
                "ts": now - (80 - i), "category": "crypto", "x": x,
                "y": y, "spread": 0.02, "feature_version": 3,
            })
        beta, n = micro.solve_weighted_ridge(rows, ridge=0.02, now=now, half_life_seconds=3600)
        self.assertEqual(n, 80)
        self.assertEqual(len(beta), 14)
        self.assertTrue(all(math.isfinite(v) for v in beta))

    def test_local_factor_v4_loading_is_causal(self):
        lf = load_script("frontier_lf_v4", "scripts/v6_local_factor_v4.py")
        target = [0.1 * i for i in range(60)]
        peers = [[0.08 * i for i in range(60)], [0.12 * i for i in range(60)]]
        r1, l1 = lf._causal_dynamic_residual(target, peers)
        changed = list(target)
        changed[-1] += 100.0
        r2, l2 = lf._causal_dynamic_residual(changed, peers)
        self.assertEqual(r1[:-1], r2[:-1])
        self.assertEqual(l1, l2)
        self.assertTrue(all(math.isfinite(v) for v in r1 + l1))

    def test_structural_v2_preserves_type_and_expiry(self):
        structural = load_script("frontier_structural_v2", "scripts/v6_relation_intents_v2.py")
        a = structural.typed_signature("Will Bitcoin be above $100,000 on August 25, 2026?")
        b = structural.typed_signature("Will Bitcoin be above $150,000 on August 31, 2026?")
        c = structural.typed_signature("Will the Fed make at least 4 rate cuts in 2026?")
        d = structural.typed_signature("Will 2 or more hurricanes make landfall in the US in 2026?")
        self.assertIsNotNone(a); self.assertIsNotNone(b); self.assertIsNotNone(c); self.assertIsNotNone(d)
        assert a and b and c and d
        self.assertNotEqual(a.family, b.family)
        self.assertEqual(a.kind, "money")
        self.assertEqual(c.threshold, 4.0)
        self.assertEqual(d.threshold, 2.0)
        self.assertIn("market_end_ts", (ROOT / "scripts/v6_relation_intents_v2.py").read_text())

    def test_external_v2_hard_rejects_cross_asset_match(self):
        ext = load_script("frontier_external_v2", "scripts/external_intelligence_v2.py")
        pm = ext.base.PmMarket(
            market_id="pm", condition_id="c", event_id="e",
            question="Will Bitcoin be above $72,000 on August 25?", description="Bitcoin threshold",
            category="crypto", end_ts=100000, liquidity=1000, volume24h=1000,
            bid=0.48, ask=0.52, mid=0.50, yes_token="y", no_token="n", resolved_outcome=None,
        )
        silver = ext.base.KMarket(
            ticker="S", event_ticker="S", title="Will silver be above $72 on August 25?",
            subtitle="", rules="", close_ts=100000, updated_ts=99900,
            bid=0.48, ask=0.52, mid=0.50, spread=0.04, volume=1000, liquidity=1000,
        )
        btc = ext.base.KMarket(
            ticker="B", event_ticker="B", title="Will Bitcoin be above $72,000 on August 25?",
            subtitle="", rules="", close_ts=100000, updated_ts=99900,
            bid=0.48, ask=0.52, mid=0.50, spread=0.04, volume=1000, liquidity=1000,
        )
        self.assertLess(ext.score_pair(pm, silver, 14)[0], 0.0)
        self.assertGreater(ext.score_pair(pm, btc, 14)[0], 0.0)

    def test_graph_state_guard_observes_joint_states_not_marginal_product(self):
        guard = load_script("frontier_bundle_state", "scripts/v6_bundle_state_guard.py")
        tape = [
            (10, "a", "SELL", 0.4, 10.0), (11, "b", "SELL", 0.4, 10.0),
            (70, "a", "SELL", 0.4, 10.0),
            (130, "b", "SELL", 0.4, 10.0),
        ]
        states = guard.empirical_states(
            tape, tokens=["a", "b"], prices=[0.4, 0.4], required=[5.0, 5.0],
            start_ts=0, end_ts=180, window_seconds=60,
        )
        self.assertEqual(states, [(True, True), (True, False), (False, True)])
        empirical_joint = sum(all(state) for state in states) / len(states)
        marginal_a = sum(state[0] for state in states) / len(states)
        marginal_b = sum(state[1] for state in states) / len(states)
        self.assertNotAlmostEqual(empirical_joint, marginal_a * marginal_b)

    def test_hard_arb_v3_requires_fresh_stable_cross_leg_snapshot(self):
        hard = load_script("frontier_hard_v3", "scripts/v6_hard_arb_paper_v3.py")
        now_ms = time.monotonic_ns() // 1_000_000
        books = [
            hard.base.Book("a", [(0.40, 100.0)], 1.0),
            hard.base.Book("b", [(0.40, 100.0)], 1.0),
        ]
        for book in books:
            book.received_ms = now_ms
            book.snapshot_stable = True
        fees = [hard.base.FeeDetails(0.0, 1.0, True, True, "test") for _ in books]
        ok = hard.max_executable_shares(
            books, fees, cash_room=100.0, max_trade_usd=100.0,
            min_edge=0.0001, slippage_bps=0.0,
        )
        self.assertIsNotNone(ok)
        books[0].received_ms = now_ms - 2500
        stale = hard.max_executable_shares(
            books, fees, cash_room=100.0, max_trade_usd=100.0,
            min_edge=0.0001, slippage_bps=0.0,
        )
        self.assertIsNone(stale)


if __name__ == "__main__":
    unittest.main()
