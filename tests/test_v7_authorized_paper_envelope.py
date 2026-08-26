from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "paper_v7.json"

MARKET_LIMIT = 1000.0
CEILINGS = {
    "max_drawdown": 0.15,
    "max_market_fraction": 0.05,
    "max_event_fraction": 0.15,
    "max_gross_fraction": 0.70,
    "fractional_kelly": 0.25,
    "max_trade_usd": 125.0,
    "hard_arb_max_trade_usd": 125.0,
}
FLOORS = {
    "min_liquidity": 2.0,
    "min_net_edge": 0.00005,
    "uncertainty_penalty": 0.0,
    "intent_min_edge": 0.00005,
    "hard_arb_min_net_edge": 0.00005,
}
SLEEVE_CEILINGS = {
    "micro_maker_capital_fraction": 0.22,
    "micro_taker_capital_fraction": 0.12,
    "relative_value_capital_fraction": 0.34,
    "hard_arb_capital_fraction": 0.22,
    "external_capital_fraction": 0.08,
    "reserve_fraction": 0.02,
}


def number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def validate(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    def ceiling(label: str, value: Any, limit: float) -> None:
        current = number(value)
        if current is None:
            errors.append(f"{label} missing or non-numeric")
        elif current > limit + 1e-12:
            errors.append(f"{label} allowed<={limit:g}, got {current:g}")

    def floor(label: str, value: Any, limit: float) -> None:
        current = number(value)
        if current is None:
            errors.append(f"{label} missing or non-numeric")
        elif current < limit - 1e-12:
            errors.append(f"{label} required>={limit:g}, got {current:g}")

    if config.get("paper_only") is not True:
        errors.append("paper_only must be true")
    ceiling("market_limit", config.get("market_limit"), MARKET_LIMIT)
    for key in ("max_drawdown", "max_market_fraction", "max_event_fraction", "max_gross_fraction", "fractional_kelly", "max_trade_usd"):
        ceiling(key, config.get(key), CEILINGS[key])
    for key in ("min_liquidity", "min_net_edge", "uncertainty_penalty"):
        floor(key, config.get(key), FLOORS[key])
    if config.get("fixed_dollar_trade_cap_enabled") is not True:
        errors.append("fixed_dollar_trade_cap_enabled must be true")

    multi = config.get("multi_strategy") if isinstance(config.get("multi_strategy"), dict) else {}
    if multi.get("paper_only") is not True:
        errors.append("multi_strategy.paper_only must be true")
    ceiling("multi_strategy.global_max_drawdown", multi.get("global_max_drawdown"), CEILINGS["max_drawdown"])
    ceiling("multi_strategy.global_max_gross_fraction", multi.get("global_max_gross_fraction"), CEILINGS["max_gross_fraction"])

    v7 = config.get("v7") if isinstance(config.get("v7"), dict) else {}
    if v7.get("paper_only") is not True:
        errors.append("v7.paper_only must be true")
    if v7.get("authenticated_execution") is not False:
        errors.append("v7.authenticated_execution must be false")
    if v7.get("authoritative_fee_required") is not True:
        errors.append("v7.authoritative_fee_required must be true")
    if v7.get("shared_execution_ledger_required") is not True:
        errors.append("v7.shared_execution_ledger_required must be true")
    if v7.get("joint_fill_state_required_for_multileg") is not True:
        errors.append("v7.joint_fill_state_required_for_multileg must be true")
    if v7.get("hard_arb_fixed_dollar_trade_cap_enabled") is not True:
        errors.append("v7.hard_arb_fixed_dollar_trade_cap_enabled must be true")
    ceiling("v7.hard_arb_max_trade_usd", v7.get("hard_arb_max_trade_usd"), CEILINGS["hard_arb_max_trade_usd"])
    for key in ("intent_min_edge", "hard_arb_min_net_edge"):
        floor(f"v7.{key}", v7.get(key), FLOORS[key])

    fractions: list[float] = []
    for key, limit in SLEEVE_CEILINGS.items():
        value = number(v7.get(key))
        if value is None or value < -1e-12:
            errors.append(f"v7.{key} missing, non-numeric or negative")
            continue
        fractions.append(value)
        ceiling(f"v7.{key}", value, limit)
    if len(fractions) == len(SLEEVE_CEILINGS) and sum(fractions) > 1.0 + 1e-12:
        errors.append(f"v7 capital allocations exceed 100%: total={sum(fractions):g}")

    return errors


def authorized_config() -> dict[str, Any]:
    return {
        "paper_only": True,
        "market_limit": 1000,
        "min_liquidity": 2.0,
        "min_net_edge": 0.00005,
        "uncertainty_penalty": 0.0,
        "fractional_kelly": 0.25,
        "fixed_dollar_trade_cap_enabled": True,
        "max_trade_usd": 125.0,
        "max_market_fraction": 0.05,
        "max_event_fraction": 0.15,
        "max_gross_fraction": 0.70,
        "max_drawdown": 0.15,
        "multi_strategy": {
            "paper_only": True,
            "global_max_drawdown": 0.15,
            "global_max_gross_fraction": 0.70,
        },
        "v7": {
            "paper_only": True,
            "micro_maker_capital_fraction": 0.22,
            "micro_taker_capital_fraction": 0.12,
            "relative_value_capital_fraction": 0.34,
            "hard_arb_capital_fraction": 0.22,
            "external_capital_fraction": 0.08,
            "reserve_fraction": 0.02,
            "intent_min_edge": 0.00005,
            "hard_arb_min_net_edge": 0.00005,
            "hard_arb_fixed_dollar_trade_cap_enabled": True,
            "hard_arb_max_trade_usd": 125.0,
            "authoritative_fee_required": True,
            "shared_execution_ledger_required": True,
            "joint_fill_state_required_for_multileg": True,
            "authenticated_execution": False,
        },
    }


class V7AuthorizedPaperEnvelopeContractTest(unittest.TestCase):
    def test_authorized_envelope_is_valid(self) -> None:
        self.assertEqual(validate(authorized_config()), [])

    def test_superseded_unbounded_v7_profile_is_rejected(self) -> None:
        config = authorized_config()
        config.update({
            "fixed_dollar_trade_cap_enabled": False,
            "max_trade_usd": 1e100,
            "max_market_fraction": 1.0,
            "max_event_fraction": 1.0,
            "max_gross_fraction": 1.0,
        })
        config["multi_strategy"]["global_max_gross_fraction"] = 1.0
        config["v7"]["hard_arb_fixed_dollar_trade_cap_enabled"] = False
        config["v7"]["hard_arb_max_trade_usd"] = 1e100
        joined = "\n".join(validate(config))
        self.assertIn("fixed_dollar_trade_cap_enabled must be true", joined)
        self.assertIn("max_trade_usd allowed<=125", joined)
        self.assertIn("max_market_fraction allowed<=0.05", joined)
        self.assertIn("max_event_fraction allowed<=0.15", joined)
        self.assertIn("max_gross_fraction allowed<=0.7", joined)
        self.assertIn("multi_strategy.global_max_gross_fraction allowed<=0.7", joined)
        self.assertIn("v7.hard_arb_fixed_dollar_trade_cap_enabled must be true", joined)
        self.assertIn("v7.hard_arb_max_trade_usd allowed<=125", joined)

    def test_hard_safety_and_execution_provenance_requirements_are_fail_closed(self) -> None:
        config = authorized_config()
        config["max_drawdown"] = 0.151
        config["v7"]["authenticated_execution"] = True
        config["v7"]["authoritative_fee_required"] = False
        config["v7"]["shared_execution_ledger_required"] = False
        config["v7"]["joint_fill_state_required_for_multileg"] = False
        joined = "\n".join(validate(config))
        self.assertIn("max_drawdown allowed<=0.15", joined)
        self.assertIn("v7.authenticated_execution must be false", joined)
        self.assertIn("v7.authoritative_fee_required must be true", joined)
        self.assertIn("v7.shared_execution_ledger_required must be true", joined)
        self.assertIn("v7.joint_fill_state_required_for_multileg must be true", joined)

    def test_each_sleeve_ceiling_binds_even_if_total_stays_one(self) -> None:
        config = authorized_config()
        config["v7"]["relative_value_capital_fraction"] = 0.35
        config["v7"]["micro_maker_capital_fraction"] = 0.21
        joined = "\n".join(validate(config))
        self.assertIn("v7.relative_value_capital_fraction allowed<=0.34, got 0.35", joined)
        self.assertNotIn("allocations exceed 100%", joined)

    def test_current_v7_config_if_present_respects_authorized_envelope(self) -> None:
        if not CONFIG.is_file():
            self.skipTest("V7 config is not present on this branch")
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        errors = validate(config)
        self.assertEqual(errors, [], "\n".join(errors))


if __name__ == "__main__":
    unittest.main()
