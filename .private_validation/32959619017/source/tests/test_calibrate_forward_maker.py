#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "calibrate_forward_maker.py"
SPEC = importlib.util.spec_from_file_location("calibrate_forward_maker", SCRIPT)
assert SPEC and SPEC.loader
MOD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MOD
SPEC.loader.exec_module(MOD)


def result(
    policy: str,
    pnl: float,
    *,
    reward: float = 0.0,
    any_fill: bool = True,
    pair_fill: bool = True,
    one_sided: bool = False,
    shares: float = 5.0,
    markout: float = 0.001,
) -> dict:
    filled = shares if any_fill else 0.0
    return {
        "market_id": f"m-{policy}",
        "condition_id": f"c-{policy}",
        "policy": policy,
        "any_fill": any_fill,
        "pair_fill": pair_fill,
        "one_sided_only": one_sided,
        "matched_shares": shares if pair_fill else 0.0,
        "conservative_pnl_ex_rewards_usd": pnl,
        "conditional_pnl_including_reward_usd": pnl + reward,
        "maker_rebate_fee_basis_usd_not_revenue": 0.02 if any_fill else 0.0,
        "yes": {
            "filled_shares": filled,
            "markout_60_bid_per_share": markout if any_fill else None,
            "markout_300_bid_per_share": markout if any_fill else None,
        },
        "no": {
            "filled_shares": filled if pair_fill else 0.0,
            "markout_60_bid_per_share": markout if pair_fill else None,
            "markout_300_bid_per_share": markout if pair_fill else None,
        },
    }


def session(index: int, rows: list[dict], spacing: int = 1800) -> dict:
    return {
        "schema": "polymarket_forward_maker_probe_v1",
        "generated_ts": 1_800_000_000 + index * spacing,
        "github_run_id": str(10_000 + index),
        "git_sha": f"sha-{index}",
        "results": rows,
    }


class ForwardMakerCalibrationTests(unittest.TestCase):
    def config(self, **overrides):
        values = dict(
            min_sessions=8,
            min_history_span_seconds=10_000,
            min_probes=80,
            min_any_fills=20,
            min_pair_fills=10,
            min_positive_active_session_rate=0.55,
            max_one_sided_given_fill_upper=0.40,
            bootstrap_reps=500,
            bootstrap_alpha=0.05,
            bootstrap_block_sessions=3,
            seed=7,
        )
        values.update(overrides)
        return MOD.GateConfig(**values)

    def test_stable_positive_ex_reward_policy_is_selected(self):
        sessions = []
        for i in range(12):
            rows = []
            for j in range(10):
                rows.append(result("join", 0.02 + 0.001 * (j % 3)))
                rows.append(
                    result(
                        "improve1",
                        -0.01,
                        one_sided=(j % 3 == 0),
                        pair_fill=(j % 3 != 0),
                    )
                )
            sessions.append(session(i, rows))
        payload = MOD.calibrate(sessions, self.config())
        self.assertTrue(payload["eligible_for_paper_shadow"])
        self.assertEqual(payload["selected_policy_for_paper_shadow"], "join")
        join = payload["by_policy"]["join"]
        self.assertTrue(join["eligible_for_paper_shadow"], join["gate_failures"])
        self.assertGreater(join["block_bootstrap_lcb_mean_pnl_ex_rewards_per_probe_usd"], 0.0)
        self.assertFalse(payload["real_money_eligible"])
        self.assertEqual(payload["production_action"], "no_change")

    def test_reward_only_profit_cannot_pass(self):
        sessions = [
            session(i, [result("join", -0.02, reward=0.10) for _ in range(10)])
            for i in range(12)
        ]
        payload = MOD.calibrate(sessions, self.config())
        report = payload["by_policy"]["join"]
        self.assertFalse(report["eligible_for_paper_shadow"])
        self.assertIn("nonpositive_ex_reward_bootstrap_lcb", report["gate_failures"])
        self.assertGreater(report["total_pnl_with_conditional_rewards_usd_not_booked"], 0.0)
        self.assertIsNone(payload["selected_policy_for_paper_shadow"])

    def test_one_sided_fill_risk_blocks_apparently_profitable_policy(self):
        sessions = []
        for i in range(12):
            rows = [
                result(
                    "improve1",
                    0.03,
                    pair_fill=j >= 8,
                    one_sided=j < 8,
                )
                for j in range(10)
            ]
            sessions.append(session(i, rows))
        payload = MOD.calibrate(
            sessions,
            self.config(min_pair_fills=5, max_one_sided_given_fill_upper=0.35),
        )
        report = payload["by_policy"]["improve1"]
        self.assertFalse(report["eligible_for_paper_shadow"])
        self.assertIn("excessive_one_sided_fill_risk", report["gate_failures"])

    def test_insufficient_no_fill_history_stays_ineligible(self):
        sessions = [
            session(
                i,
                [
                    result(
                        "fade1",
                        0.0,
                        any_fill=False,
                        pair_fill=False,
                        shares=0.0,
                    )
                    for _ in range(10)
                ],
            )
            for i in range(12)
        ]
        payload = MOD.calibrate(sessions, self.config())
        failures = payload["by_policy"]["fade1"]["gate_failures"]
        self.assertIn("insufficient_any_fills", failures)
        self.assertIn("no_active_fill_sessions", failures)
        self.assertIn("nonpositive_ex_reward_bootstrap_lcb", failures)

    def test_duplicate_workflow_runs_do_not_inflate_evidence(self):
        duplicate = session(0, [result("join", 0.02) for _ in range(10)])
        payload = MOD.calibrate(
            [duplicate, duplicate],
            self.config(
                min_sessions=2,
                min_history_span_seconds=1,
                min_probes=20,
                min_any_fills=2,
                min_pair_fills=2,
            ),
        )
        report = payload["by_policy"]["join"]
        self.assertEqual(payload["history"]["valid_sessions"], 1)
        self.assertEqual(payload["history"]["duplicate_sessions_dropped"], 1)
        self.assertEqual(report["probes"], 10)
        self.assertIn("insufficient_sessions", report["gate_failures"])
        self.assertIn("insufficient_history_span", report["gate_failures"])
        self.assertIn("insufficient_probes", report["gate_failures"])

    def test_history_span_gate_prevents_many_correlated_burst_runs(self):
        sessions = [
            session(i, [result("join", 0.03) for _ in range(20)], spacing=60)
            for i in range(12)
        ]
        payload = MOD.calibrate(
            sessions,
            self.config(min_sessions=12, min_history_span_seconds=18_000),
        )
        report = payload["by_policy"]["join"]
        self.assertFalse(report["eligible_for_paper_shadow"])
        self.assertIn("insufficient_history_span", report["gate_failures"])

    def test_compact_session_round_trip_preserves_policy_sufficient_statistics(self):
        raw_sessions = []
        compact_sessions = []
        for i in range(12):
            raw = session(
                i,
                [
                    result("join", 0.02, reward=0.10),
                    result("join", 0.01, markout=-0.002),
                    result("fade1", -0.03, pair_fill=False, one_sided=True),
                ],
            )
            raw_sessions.append(raw)
            compact = MOD.compact_session(raw)
            self.assertNotIn("results", compact)
            self.assertEqual(compact["schema"], "polymarket_forward_maker_session_summary_v1")
            compact_sessions.append(compact)

        raw_payload = MOD.calibrate(
            raw_sessions,
            self.config(min_probes=20, min_any_fills=10, min_pair_fills=10),
        )
        compact_payload = MOD.calibrate(
            compact_sessions,
            self.config(min_probes=20, min_any_fills=10, min_pair_fills=10),
        )
        for policy in ("join", "fade1"):
            raw = raw_payload["by_policy"][policy]
            compact = compact_payload["by_policy"][policy]
            for key in (
                "probes",
                "any_fills",
                "pair_fills",
                "one_sided_only",
                "total_pnl_ex_rewards_usd",
                "total_conditional_rewards_usd_not_booked",
                "matched_shares",
                "filled_share_weighted_markout_60_bid_per_share",
            ):
                self.assertEqual(raw[key], compact[key], (policy, key))

    def test_block_bootstrap_is_deterministic(self):
        summaries = []
        for i in range(12):
            rows = [result("join", 0.04 if i < 6 else -0.005) for _ in range(10)]
            summaries.extend(MOD.summarize_session(session(i, rows), i))
        first = MOD.block_bootstrap_mean_per_probe(summaries, 500, 0.05, 11, 4)
        second = MOD.block_bootstrap_mean_per_probe(summaries, 500, 0.05, 11, 4)
        self.assertEqual(first, second)

    def test_loader_accepts_compact_and_skips_malformed_lines(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            history = td / "history.jsonl"
            raw = session(0, [result("join", 0.01)])
            compact = MOD.compact_session(session(1, [result("join", 0.02)]))
            history.write_text(
                json.dumps(raw)
                + "\n"
                + json.dumps(compact)
                + "\n"
                + "{bad json\n"
                + json.dumps({"not_results": []})
                + "\n",
                encoding="utf-8",
            )
            loaded, malformed = MOD.load_history(history)
            self.assertEqual(len(loaded), 2)
            self.assertEqual(malformed, 2)

    def test_cli_writes_atomic_json(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            history = td / "history.jsonl"
            output = td / "calibration.json"
            good = session(0, [result("join", 0.01)])
            history.write_text(json.dumps(good) + "\n", encoding="utf-8")
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--history",
                    str(history),
                    "--output",
                    str(output),
                    "--min-sessions",
                    "1",
                    "--min-history-span-seconds",
                    "0",
                    "--min-probes",
                    "1",
                    "--min-any-fills",
                    "1",
                    "--min-pair-fills",
                    "1",
                    "--bootstrap-reps",
                    "100",
                    "--bootstrap-block-sessions",
                    "1",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["history"]["malformed_lines"], 0)
            self.assertEqual(payload["schema"], "polymarket_forward_maker_calibration_v2")
            self.assertFalse(payload["real_money_eligible"])
            self.assertEqual(payload["production_action"], "no_change")


if __name__ == "__main__":
    unittest.main()
