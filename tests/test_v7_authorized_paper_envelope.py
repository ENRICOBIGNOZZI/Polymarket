from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "paper_v7.json"
DIRECTIVES = ROOT / "config" / "operator_directives.json"


def number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def authorization() -> dict[str, Any]:
    data = json.loads(DIRECTIVES.read_text(encoding="utf-8"))
    return dict(data["paper_v7_authorization"])


def validate(config: dict[str, Any]) -> list[str]:
    auth = authorization()
    errors: list[str] = []

    def require_exact(label: str, value: Any, expected: float) -> None:
        current = number(value)
        if current is None or abs(current - expected) > 1e-12:
            errors.append(f"{label} required={expected:g}, got {value!r}")

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
    if config.get("fixed_dollar_trade_cap_enabled") is not False:
        errors.append("fixed_dollar_trade_cap_enabled must be false for V7 PAPER")

    ceiling("market_limit", config.get("market_limit"), float(auth["market_limit"]))
    floor("min_liquidity", config.get("min_liquidity"), float(auth["min_liquidity"]))
    floor("min_net_edge", config.get("min_net_edge"), float(auth["min_net_edge"]))
    floor("uncertainty_penalty", config.get("uncertainty_penalty"), float(auth["uncertainty_penalty"]))
    ceiling("fractional_kelly", config.get("fractional_kelly"), float(auth["fractional_kelly_ceiling"]))
    ceiling("max_drawdown", config.get("max_drawdown"), float(auth["max_drawdown"]))

    for key in ("max_trade_fraction", "max_market_fraction", "max_event_fraction", "max_gross_fraction"):
        require_exact(key, config.get(key), float(auth[key]))

    finite_guard = number(config.get("max_trade_usd"))
    authorized_guard = float(auth["max_trade_usd_disabled_cap_value"])
    if finite_guard is None or not 0.0 < finite_guard <= authorized_guard:
        errors.append(
            f"max_trade_usd must be a positive finite defense-in-depth guard <= {authorized_guard:g}"
        )

    multi = config.get("multi_strategy") if isinstance(config.get("multi_strategy"), dict) else {}
    if multi.get("paper_only") is not True:
        errors.append("multi_strategy.paper_only must be true")
    ceiling("multi_strategy.global_max_drawdown", multi.get("global_max_drawdown"), float(auth["max_drawdown"]))
    require_exact("multi_strategy.global_max_gross_fraction", multi.get("global_max_gross_fraction"), float(auth["max_gross_fraction"]))

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
    if v7.get("hard_arb_fixed_dollar_trade_cap_enabled") is not False:
        errors.append("v7.hard_arb_fixed_dollar_trade_cap_enabled must be false")
    require_exact("v7.hard_arb_max_trade_fraction", v7.get("hard_arb_max_trade_fraction"), float(auth["hard_arb_max_trade_fraction"]))
    hard_guard = number(v7.get("hard_arb_max_trade_usd"))
    authorized_hard_guard = float(auth["hard_arb_max_trade_usd_disabled_cap_value"])
    if hard_guard is None or not 0.0 < hard_guard <= authorized_hard_guard:
        errors.append(
            "v7.hard_arb_max_trade_usd must be a positive finite "
            f"defense-in-depth guard <= {authorized_hard_guard:g}"
        )
    floor("v7.intent_min_edge", v7.get("intent_min_edge"), float(auth["min_net_edge"]))
    floor("v7.hard_arb_min_net_edge", v7.get("hard_arb_min_net_edge"), float(auth["min_net_edge"]))

    if v7.get("capital_authority_owner") != "V7_CANONICAL_ALLOCATOR":
        errors.append("v7.capital_authority_owner must be V7_CANONICAL_ALLOCATOR")
    engine_fractions = v7.get("engine_capital_fractions") if isinstance(
        v7.get("engine_capital_fractions"), dict
    ) else {}
    if set(engine_fractions) != {"BTC_SETTLEMENT_ENGINE", "STRUCTURAL_ARB_ENGINE"}:
        errors.append("v7.engine_capital_fractions must contain exactly two engines")
    sleeve_keys = (*sorted(engine_fractions), "reserve_fraction")
    fractions: list[float] = []
    for key in sleeve_keys:
        value = number(v7.get(key) if key == "reserve_fraction" else engine_fractions.get(key))
        if value is None or value < -1e-12:
            errors.append(f"v7.{key} missing, non-numeric or negative")
        else:
            fractions.append(value)
    if len(fractions) == len(sleeve_keys) and sum(fractions) > 1.0 + 1e-9:
        errors.append(f"v7 engine capital plus reserve exceeds 100%: total={sum(fractions):g}")
    if any(key.endswith("_capital_fraction") for key in v7):
        errors.append("component strategy capital fractions are forbidden")
    return errors


def authorized_config() -> dict[str, Any]:
    return {
        "paper_only": True,
        "market_limit": 1000,
        "min_liquidity": 2.0,
        "min_net_edge": 0.00005,
        "uncertainty_penalty": 0.0,
        "fractional_kelly": 0.25,
        "fixed_dollar_trade_cap_enabled": False,
        "max_trade_usd": 300.0,
        "max_trade_fraction": 1.0,
        "max_market_fraction": 1.0,
        "max_event_fraction": 1.0,
        "max_gross_fraction": 1.0,
        "max_drawdown": 0.15,
        "multi_strategy": {
            "paper_only": True,
            "global_max_drawdown": 0.15,
            "global_max_gross_fraction": 1.0,
        },
        "v7": {
            "paper_only": True,
            "capital_authority_owner": "V7_CANONICAL_ALLOCATOR",
            "engine_capital_fractions": {
                "BTC_SETTLEMENT_ENGINE": 0.40,
                "STRUCTURAL_ARB_ENGINE": 0.20,
            },
            "component_observation_budget_fractions": {
                "professional_maker": 0.20,
                "crypto_informed_taker": 0.0,
                "fast_structural": 0.10,
            },
            "research_compute_budget_fractions": {},
            "reserve_fraction": 0.02,
            "intent_min_edge": 0.00005,
            "hard_arb_min_net_edge": 0.00005,
            "hard_arb_fixed_dollar_trade_cap_enabled": False,
            "hard_arb_max_trade_usd": 300.0,
            "hard_arb_max_trade_fraction": 1.0,
            "authoritative_fee_required": True,
            "shared_execution_ledger_required": True,
            "joint_fill_state_required_for_multileg": True,
            "authenticated_execution": False,
        },
    }


class V7AuthorizedPaperEnvelopeContractTest(unittest.TestCase):
    def test_operator_directive_is_the_source_of_truth(self) -> None:
        auth = authorization()
        self.assertFalse(auth["fixed_dollar_trade_cap_enabled"])
        self.assertFalse(auth["hard_arb_fixed_dollar_trade_cap_enabled"])
        self.assertEqual(float(auth["max_trade_usd_disabled_cap_value"]), 300.0)
        self.assertEqual(float(auth["hard_arb_max_trade_usd_disabled_cap_value"]), 300.0)
        for key in ("max_trade_fraction", "max_market_fraction", "max_event_fraction", "max_gross_fraction", "hard_arb_max_trade_fraction"):
            self.assertEqual(float(auth[key]), 1.0)
        self.assertFalse(auth["authenticated_execution"])

    def test_authorized_v7_100_percent_envelope_is_valid(self) -> None:
        self.assertEqual(validate(authorized_config()), [])

    def test_obsolete_bounded_policy_is_rejected(self) -> None:
        config = authorized_config()
        config.update({
            "fixed_dollar_trade_cap_enabled": True,
            "max_trade_usd": 125.0,
            "max_market_fraction": 0.05,
            "max_event_fraction": 0.15,
            "max_gross_fraction": 0.70,
        })
        config["multi_strategy"]["global_max_gross_fraction"] = 0.70
        config["v7"]["hard_arb_fixed_dollar_trade_cap_enabled"] = True
        config["v7"]["hard_arb_max_trade_usd"] = 125.0
        joined = "\n".join(validate(config))
        self.assertIn("fixed_dollar_trade_cap_enabled must be false", joined)
        self.assertIn("max_market_fraction required=1", joined)
        self.assertIn("max_event_fraction required=1", joined)
        self.assertIn("max_gross_fraction required=1", joined)
        self.assertIn("v7.hard_arb_fixed_dollar_trade_cap_enabled must be false", joined)

    def test_economic_and_execution_safety_still_bind(self) -> None:
        config = authorized_config()
        config["max_drawdown"] = 0.151
        config["fractional_kelly"] = 0.251
        config["min_net_edge"] = 0.0
        config["v7"]["authenticated_execution"] = True
        config["v7"]["authoritative_fee_required"] = False
        config["v7"]["shared_execution_ledger_required"] = False
        config["v7"]["joint_fill_state_required_for_multileg"] = False
        joined = "\n".join(validate(config))
        self.assertIn("max_drawdown allowed<=0.15", joined)
        self.assertIn("fractional_kelly allowed<=0.25", joined)
        self.assertIn("min_net_edge required>=5e-05", joined)
        self.assertIn("v7.authenticated_execution must be false", joined)
        self.assertIn("v7.authoritative_fee_required must be true", joined)
        self.assertIn("v7.shared_execution_ledger_required must be true", joined)
        self.assertIn("v7.joint_fill_state_required_for_multileg must be true", joined)

    def test_current_v7_config_if_present_respects_operator_authorization(self) -> None:
        if not CONFIG.is_file():
            self.skipTest("V7 config is not present on this branch")
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        errors = validate(config)
        self.assertEqual(errors, [], "\n".join(errors))


if __name__ == "__main__":
    unittest.main()
