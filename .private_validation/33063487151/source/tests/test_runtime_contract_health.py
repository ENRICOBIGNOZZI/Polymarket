from __future__ import annotations

import csv
import json
import tempfile
import time
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from runtime_contract_health import ContractError, validate


class RuntimeContractHealthTests(unittest.TestCase):
    def _fixture(self, base: Path, *, version: int, nested: bool) -> tuple[Path, Path]:
        config = base / "config"
        run_root = base / "runs" / f"paper_v{version}_live"
        state_root = run_root / "execution" if nested else run_root
        config.mkdir(parents=True)
        state_root.mkdir(parents=True)
        (config / "live_champion.json").write_text(json.dumps({
            "version": version,
            "run_root": f"runs/paper_v{version}_live",
            "config": f"config/paper_v{version}.json",
        }), encoding="utf-8")
        (config / f"paper_v{version}.json").write_text(json.dumps({
            "starting_capital": 10000,
            "max_drawdown": 0.15,
        }), encoding="utf-8")
        now = int(time.time())
        (state_root / "runtime_status.json").write_text(json.dumps({
            "timestamp": now,
            "version": version,
            "paper_only": True,
            "authenticated_execution": False,
            "starting_capital": 10000,
            "equity": 10010,
            "pnl": 10,
            "drawdown": 0.01,
            "live_units": 1,
            "gross_exposure": 25,
            "strategies": {"model": {"equity": 10010}},
        }), encoding="utf-8")
        (state_root / "allocator_status.json").write_text(json.dumps({
            "timestamp": now,
            "paper_only": True,
            "authenticated_execution": False,
            "models_expected": 1,
            "models_alive": 1,
        }), encoding="utf-8")
        with (state_root / "strategy_status.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["name", "alive", "status_age_seconds"])
            writer.writeheader()
            writer.writerow({"name": "model", "alive": 1, "status_age_seconds": 0})
        return config / "live_champion.json", state_root

    def test_direct_runtime_layout_is_valid(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            manifest, _ = self._fixture(base, version=6, nested=False)
            result = validate(manifest, base, 180)
            self.assertEqual(result["version"], 6)
            self.assertEqual(result["state_root"], "runs/paper_v6_live")

    def test_nested_runtime_layout_is_version_agnostic(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            manifest, _ = self._fixture(base, version=99, nested=True)
            result = validate(manifest, base, 180)
            self.assertEqual(result["version"], 99)
            self.assertEqual(result["state_root"], "runs/paper_v99_live/execution")

    def test_version_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            manifest, state = self._fixture(base, version=7, nested=True)
            payload = json.loads((state / "runtime_status.json").read_text())
            payload["version"] = 6
            (state / "runtime_status.json").write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "runtime_version_mismatch"):
                validate(manifest, base, 180)

    def test_authenticated_execution_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            manifest, state = self._fixture(base, version=7, nested=True)
            payload = json.loads((state / "runtime_status.json").read_text())
            payload["authenticated_execution"] = True
            (state / "runtime_status.json").write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "authenticated_execution_enabled"):
                validate(manifest, base, 180)

    def test_stale_runtime_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            manifest, state = self._fixture(base, version=7, nested=True)
            payload = json.loads((state / "runtime_status.json").read_text())
            payload["timestamp"] = int(time.time()) - 1000
            (state / "runtime_status.json").write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "runtime_status_stale"):
                validate(manifest, base, 180)

    def test_drawdown_limit_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            manifest, state = self._fixture(base, version=7, nested=True)
            payload = json.loads((state / "runtime_status.json").read_text())
            payload["drawdown"] = 0.16
            (state / "runtime_status.json").write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "drawdown_limit_breached"):
                validate(manifest, base, 180)


if __name__ == "__main__":
    unittest.main()
