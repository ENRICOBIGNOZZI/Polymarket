from __future__ import annotations
import copy
import json
import math
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "monitoring"))
import exporter_v7 as exporter
from v7_operator_truth import operator_summary, source_status
import build_operator_dashboard as builder


def fixture():
    safety = {"paper_only": True, "authenticated_execution": False, "real_order_submission": False}
    return {
        "timestamp": 1000, "sha": "a" * 40,
        "runtime": {**safety, "schema": "polymarket_v7_runtime_status_v3", "model_sha": "a"*40, "timestamp": 995},
        "portfolio": {**safety, "schema": "polymarket_v7_portfolio_guard_v2", "timestamp": 997, "equity": 1000.0, "account_starting_capital": 1000.0, "drawdown": 0.0},
        "canonical_economics": {**safety, "schema": "polymarket_v7_canonical_economics_v1", "expected_model_sha": "a"*40, "net_pnl": 0.0},
        "ledger": {"present": True, "valid": True, "model_shas": ["a"*40], "total": {"finals": 0, "markout_sum": {"1s": 0.0}, "markout_count": {"1s": 0}}},
        "reconciliation": {"reconciled": True, "reason_codes": []},
        "ages": {"economics": 10.0},
        "operations": {"disk_free_ratio": 0.4, "supervisor_alive": True, "single_writer": True, "ledger_writable": True},
        "lead_lag": {**safety, "schema": "polymarket_v7_lead_lag_taker_v1_status", "model_sha": "a"*40, "timestamp_ms": 999000, "automatic_promotion": False, "state": "COLLECTING", "entries": 0, "realized_pnl": 0.0},
    }


class OperatorTruthTests(unittest.TestCase):
    def test_missing_nan_and_infinite_are_not_zero(self):
        for value in (None, "bad", math.nan):
            self.assertEqual(exporter._metric("m", value), "m NaN")
        self.assertEqual(exporter._metric("m", math.inf), "m +Inf")
        self.assertEqual(exporter._metric("m", -math.inf), "m -Inf")
        self.assertEqual(exporter._metric("m", 0), "m 0")
        self.assertEqual(exporter._metric("m", False), "m 0")

    def test_valid_observed_zero_is_verified(self):
        summary = operator_summary(fixture(), [])
        self.assertTrue(summary["accounting_verified"])
        self.assertFalse(summary["attention_required"])

    def test_missing_source_is_not_fresh(self):
        for key in ("runtime", "portfolio", "canonical_economics"):
            with self.subTest(key=key):
                snapshot = fixture(); snapshot.pop(key)
                self.assertFalse(operator_summary(snapshot, [])["accounting_verified"])

    def test_malformed_numeric_portfolio_fails_closed(self):
        for value in (None, "bad", float("nan"), True):
            snapshot = fixture(); snapshot["portfolio"]["equity"] = value
            self.assertFalse(source_status(snapshot)["portfolio"]["valid"])

    def test_stale_and_future_source_fail_closed(self):
        for timestamp in (0, 1, 1100):
            snapshot = fixture(); snapshot["portfolio"]["timestamp"] = timestamp
            self.assertFalse(operator_summary(snapshot, [])["accounting_verified"])

    def test_missing_pnl_is_not_verified_zero(self):
        snapshot = fixture(); snapshot["canonical_economics"]["net_pnl"] = None
        self.assertFalse(operator_summary(snapshot, [])["accounting_verified"])

    def test_ledger_wrong_sha_fails_closed(self):
        snapshot = fixture(); snapshot["ledger"]["model_shas"] = ["b"*40]
        self.assertFalse(operator_summary(snapshot, [])["ledger_current"])

    def test_runtime_wrong_sha_fails_closed(self):
        snapshot = fixture(); snapshot["sha"] = "b"*40
        self.assertFalse(source_status(snapshot)["runtime"]["fresh"])

    def test_reconciliation_divergence_visible_without_changing_runtime_health(self):
        snapshot = fixture(); snapshot["reconciliation"] = {"reconciled": False, "reason_codes": ["pnl_divergence"]}
        result = operator_summary(snapshot, [])
        self.assertFalse(result["accounting_verified"])
        self.assertIn("pnl_divergence", result["reasons"])

    def test_disk_pressure_and_missing_owner_visible(self):
        snapshot = fixture(); snapshot["operations"]["disk_free_ratio"] = 0.06
        snapshot["operations"]["single_writer"] = False
        result = operator_summary(snapshot, [])
        self.assertIn("disk_pressure", result["reasons"])
        self.assertIn("single_writer_not_verified", result["reasons"])

    def test_unsafe_or_wrong_sha_forward_test_invalid(self):
        for key, value in (("automatic_promotion", True), ("real_order_submission", True), ("model_sha", "b"*40)):
            snapshot = fixture(); snapshot["lead_lag"][key] = value
            self.assertFalse(source_status(snapshot)["lead_lag"]["valid"])

    def test_markout_without_samples_is_nan(self):
        result = exporter.render_prometheus(fixture())
        self.assertIn('polymarket_execution_mean_markout{horizon="1s"} NaN', result)
        self.assertIn('polymarket_execution_markout_observations{horizon="1s"} 0', result)

    def test_real_zero_markout_remains_zero(self):
        snapshot = fixture(); snapshot["ledger"]["total"]["markout_count"]["1s"] = 3
        self.assertIn('polymarket_execution_mean_markout{horizon="1s"} 0', exporter.render_prometheus(snapshot))

    def test_stale_cache_overrides_frozen_health(self):
        cached = {"ready": True, "age_seconds": 46, "metrics": b"polymarket_v7_health 1\npolymarket_v7_exporter_snapshot_refresh_errors_total 0\n", "refresh_errors": 3, "last_error": "failure"}
        text = exporter.render_cached_prometheus(cached).decode()
        self.assertIn("polymarket_v7_health 0", text)
        self.assertIn("polymarket_v7_exporter_snapshot_usable 0", text)
        self.assertIn("polymarket_v7_exporter_snapshot_refresh_errors_total 3", text)
        self.assertEqual(text.count("polymarket_v7_exporter_snapshot_refresh_errors_total "), 1)

    def test_fresh_cache_stays_usable(self):
        text = exporter.render_cached_prometheus({"ready": True, "age_seconds": 2, "metrics": b"polymarket_v7_health 1\n"}).decode()
        self.assertIn("polymarket_v7_exporter_snapshot_usable 1", text)


def panels(items):
    for p in items:
        yield p
        yield from panels(p.get("panels", []))


class DashboardTruthTests(unittest.TestCase):
    def test_all_current_stat_queries_are_instant_and_never_last_not_null(self):
        for path in (ROOT / "monitoring/grafana/dashboards").glob("*.json"):
            dashboard = json.loads(path.read_text())
            ids = [p["id"] for p in panels(dashboard["panels"])]
            self.assertEqual(len(ids), len(set(ids)), path.name)
            for p in panels(dashboard["panels"]):
                if p["type"] == "stat":
                    self.assertEqual(p["options"]["reduceOptions"]["calcs"], ["last"])
                    for t in p.get("targets", []):
                        self.assertTrue(t["instant"])
                        self.assertFalse(t["range"])
                for t in p.get("targets", []):
                    self.assertIn('job="polymarket-v7"', t["expr"])
                    self.assertIn('instance="$instance"', t["expr"])
                    if p.get("title") != "Snapshot age":
                        self.assertIn("polymarket_v7_exporter_snapshot_usable", t["expr"])
                if p["type"] == "timeseries":
                    self.assertIs(p["fieldConfig"]["defaults"]["custom"]["spanNulls"], False)

    def test_completion_is_undefined_without_submissions(self):
        d = json.loads((ROOT / "monitoring/grafana/dashboards/polymarket-v7.json").read_text())
        p = next(p for p in panels(d["panels"]) if p["title"] == "Economic Completion Rate")
        self.assertIn('> 0)', p["targets"][0]["expr"])
        self.assertNotIn('lastNotNull', json.dumps(d))

    def test_builder_matches_checked_in_main_dashboard(self):
        d = json.loads((ROOT / "monitoring/grafana/dashboards/polymarket-v7.json").read_text())
        self.assertEqual(d["panels"], builder.build() + builder.diagnostics())

    def test_mixed_pnl_chart_requires_fresh_sources(self):
        expr = builder.query("polymarket_runtime_pnl_usd", history=True)
        self.assertIn('source="portfolio"', expr)
        self.assertIn("group_left(run_id)", expr)
        expr = builder.query("polymarket_execution_fills", history=True)
        self.assertIn("polymarket_v7_ledger_current_runtime", expr)

    def test_main_layout_has_no_overlapping_siblings(self):
        def check(items):
            for i, a in enumerate(items):
                ax, ay, aw, ah = (a['gridPos'][k] for k in ('x','y','w','h'))
                self.assertTrue(0 <= ax < 24 and aw > 0 and ax+aw <= 24)
                for b in items[i+1:]:
                    bx, by, bw, bh = (b['gridPos'][k] for k in ('x','y','w','h'))
                    self.assertFalse(ax < bx+bw and bx < ax+aw and ay < by+bh and by < ay+ah, (a['id'],b['id']))
                check(a.get('panels',[]))
        check(builder.build()+builder.diagnostics())
