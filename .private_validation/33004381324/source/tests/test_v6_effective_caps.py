from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.hard_safety_policy import V6_AUTHORIZED_CEILINGS


ROOT = Path(__file__).resolve().parents[1]


class V6EffectiveCapPolicyTest(unittest.TestCase):
    def test_maker_runtime_cannot_raise_market_cap_above_authorized_ceiling(self) -> None:
        cfg = json.loads((ROOT / "config" / "paper_v6.json").read_text(encoding="utf-8"))
        v6 = cfg["v6"]
        configured_market_cap = float(cfg["max_market_fraction"])
        authorized_market_cap = float(V6_AUTHORIZED_CEILINGS["max_market_fraction"])
        maker_capital = float(cfg["starting_capital"]) * float(v6["micro_maker_capital_fraction"])
        self.assertGreater(maker_capital, 0.0)

        effective_market_cap = configured_market_cap
        loop = (ROOT / "scripts" / "paper_v6_loop.sh").read_text(encoding="utf-8")
        if "child['max_market_fraction']=max(" in loop:
            trade_cap = float(cfg["max_trade_usd"])
            effective_market_cap = max(
                configured_market_cap,
                min(1.0, trade_cap / maker_capital),
            )

        self.assertLessEqual(
            effective_market_cap,
            authorized_market_cap + 1e-12,
            (
                "effective V6 maker market concentration exceeds the authorized PAPER cap: "
                f"effective={effective_market_cap:.9f}, authorized={authorized_market_cap:.9f}. "
                "max_trade_usd is a ceiling and must not override the 5% market cap. "
                "Repair the sensitive runtime materialization through research -> trusted governance -> integration."
            ),
        )


if __name__ == "__main__":
    unittest.main()
