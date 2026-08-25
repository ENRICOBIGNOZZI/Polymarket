from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "monitoring"))

from exporter_latest import LatestCollector, detect_run_root, v6_runtime_data_health


class _ProxyResponse:
    status = 200

    def __init__(self, payload: object):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class LatestExporterTest(unittest.TestCase):
    def _config(self, path: Path, capital: float = 10000.0):
        path.write_text(json.dumps({"starting_capital": capital, "max_drawdown": 0.15}), encoding="utf-8")

    def test_auto_selects_highest_engine_version_and_contract(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            runs, config = base / "runs", base / "config"
            (runs / "paper_v4_live").mkdir(parents=True)
            (runs / "paper_v5_live").mkdir(parents=True)
            config.mkdir()
            self._config(config / "paper_v4.json")
            self._config(config / "paper_v5.json")
            (runs / "paper_v4_live" / "runtime_status.json").write_text(json.dumps({"equity": 10004, "pnl": 4}), encoding="utf-8")
            (runs / "paper_v5_live" / "runtime_status.json").write_text(json.dumps({
                "equity": 10025,
                "pnl": 25,
                "drawdown": 0.01,
                "killed": False,
                "live_units": 2,
                "reserved_cash": 12,
                "gross_exposure": 40,
                "realized_pnl": 18,
                "execution_imbalance": 0.15,
                "execution_staleness": 3,
                "oos": {"trades": 40, "net_pnl": 30, "stressed_net_pnl": 20, "max_drawdown": 0.04,
                        "bootstrap_pvalue": 0.03, "eligible_for_tiny_pilot": True, "production_threshold": 0.004}
            }), encoding="utf-8")
            root, version = detect_run_root(runs, "auto")
            self.assertEqual(root.name, "paper_v5_live")
            self.assertEqual(version, (5,))
            text = LatestCollector(runs, config, "auto", None, 10).collect()
            self.assertIn('version="v5"', text)
            self.assertIn("polymarket_runtime_equity_usd 10025", text)
            self.assertIn("polymarket_runtime_realized_pnl_usd_total 18", text)
            self.assertIn("polymarket_runtime_oos_eligible 1", text)
            self.assertIn("polymarket_runtime_production_threshold 0.004", text)

    def test_explicit_run_pins_historical_version(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            runs, config = base / "runs", base / "config"
            (runs / "paper_v4_live").mkdir(parents=True)
            (runs / "paper_v6_live").mkdir(parents=True)
            config.mkdir()
            self._config(config / "paper_v4.json")
            self._config(config / "paper_v6.json")
            (runs / "paper_v4_live" / "runtime_status.json").write_text(json.dumps({"equity": 9999, "pnl": -1}), encoding="utf-8")
            (runs / "paper_v6_live" / "runtime_status.json").write_text(json.dumps({"equity": 11000, "pnl": 1000}), encoding="utf-8")
            text = LatestCollector(runs, config, "paper_v4_live", None, 10).collect()
            self.assertIn('version="v4"', text)
            self.assertIn("polymarket_runtime_equity_usd 9999", text)

    def test_v4_fallback_without_contract_file(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            runs, config = base / "runs", base / "config"
            root = runs / "paper_v4_live"
            root.mkdir(parents=True)
            config.mkdir()
            self._config(config / "paper_v4.json")
            (root / "trade_tape.csv").write_text("timestamp,asset_id,side,price,size\n1,t,BUY,0.5,1\n", encoding="utf-8")
            (root / "multileg_equity.csv").write_text(
                "timestamp,cash,equity,reserved_cash,gross_entry_cash,peak_equity,drawdown,killed,live_bundles\n"
                "1,9990,10010,3,20,10010,0,0,1\n", encoding="utf-8")
            (root / "multileg_legs.csv").write_text(
                "bundle_id,target_shares,filled_shares\n"
                "b,10,9\n"
                "b,10,4\n", encoding="utf-8")
            (root / "bundle_ledger.csv").write_text("net_pnl\n2.5\n", encoding="utf-8")
            (root / "walk_forward.json").write_text(json.dumps({
                "eligible_for_tiny_pilot": False,
                "production_threshold": 0.003,
                "bootstrap_one_sided_pvalue": 0.2,
                "oos": {"trades": 12, "net_pnl": 4, "max_drawdown": 0.02},
                "oos_cost_stress": {"net_pnl": -1}
            }), encoding="utf-8")
            text = LatestCollector(runs, config, "auto", None, 10).collect()
            self.assertIn("polymarket_runtime_equity_usd 10010", text)
            self.assertIn("polymarket_runtime_execution_imbalance_ratio 0.5", text)
            self.assertIn("polymarket_runtime_realized_pnl_usd_total 2.5", text)
            self.assertIn("polymarket_runtime_oos_stressed_net_pnl_usd -1", text)

    def test_v6_data_health_rejects_unreachable_proxy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "trade_recorder.log").write_text("trade_recorder markets=1 trades=1\n", encoding="utf-8")
            with mock.patch("exporter_latest.urllib.request.urlopen", side_effect=OSError("down")):
                healthy, reason = v6_runtime_data_health(root, proxy_port=9120)
            self.assertFalse(healthy)
            self.assertEqual(reason, "v6_market_proxy_unhealthy")

    def test_v6_data_health_rejects_all_failure_recorder_tail_and_recovers(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = root / "trade_recorder.log"
            log.write_text(
                "\n".join(
                    f"fatal: HTTP request failed: timeout {i}" for i in range(5)
                ) + "\n",
                encoding="utf-8",
            )
            response = _ProxyResponse([{"id": "market"}])
            with mock.patch("exporter_latest.urllib.request.urlopen", return_value=response):
                healthy, reason = v6_runtime_data_health(root, proxy_port=9120)
            self.assertFalse(healthy)
            self.assertEqual(reason, "v6_recorder_data_path_unhealthy")

            with log.open("a", encoding="utf-8") as handle:
                handle.write("trade_recorder markets=1 trades=1\n")
            with mock.patch("exporter_latest.urllib.request.urlopen", return_value=response):
                healthy, reason = v6_runtime_data_health(root, proxy_port=9120)
            self.assertTrue(healthy)
            self.assertEqual(reason, "ok")


if __name__ == "__main__":
    unittest.main()
