from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.hard_safety_policy import V6_AUTHORIZED_CEILINGS


ROOT = Path(__file__).resolve().parents[1]


class V6EffectiveCapPolicyTest(unittest.TestCase):
    def test_maker_runtime_cannot_raise_market_cap_above_authorized_ceiling(self) -> None:
        cfg = json.loads((ROOT / "config" / "paper_v6.json").read_text(encoding="utf-8"))
        configured_market_cap = float(cfg["max_market_fraction"])
        authorized_market_cap = float(V6_AUTHORIZED_CEILINGS["max_market_fraction"])
        loop = (ROOT / "scripts" / "paper_v6_loop.sh").read_text(encoding="utf-8")
        self.assertNotIn("child['max_market_fraction']=max(", loop)
        self.assertLessEqual(configured_market_cap, authorized_market_cap + 1e-12)

    def test_materializer_preserves_parent_market_fraction_for_every_sleeve(self) -> None:
        cfg = json.loads((ROOT / "config" / "paper_v6.json").read_text(encoding="utf-8"))
        parent_cap = float(cfg["max_market_fraction"])
        with tempfile.TemporaryDirectory() as temp_dir:
            run_root = Path(temp_dir) / "run"
            completed = subprocess.run(
                [
                    "python3",
                    str(ROOT / "scripts" / "v6_materialize_configs.py"),
                    "--config",
                    str(ROOT / "config" / "paper_v6.json"),
                    "--run-root",
                    str(run_root),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            for name in ("maker", "micro_taker", "broker", "hard_arb", "external"):
                child = json.loads((run_root / f"{name}_config.json").read_text(encoding="utf-8"))
                self.assertAlmostEqual(float(child["max_market_fraction"]), parent_cap, places=12)

    def test_materializer_does_not_convert_dollar_trade_ceiling_into_concentration(self) -> None:
        text = (ROOT / "scripts" / "v6_materialize_configs.py").read_text(encoding="utf-8")
        self.assertNotIn("trade_cap/child", text.replace(" ", ""))
        self.assertNotIn("max(float(child.get(\"max_market_fraction\"", text)
        self.assertIn("max_trade_usd is a ceiling", text)


if __name__ == "__main__":
    unittest.main()
