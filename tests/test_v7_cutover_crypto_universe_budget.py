from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from v7_cutover_contract import validate_crypto_universe_resource_budget  # noqa: E402


def config() -> dict:
    return json.loads((ROOT / "config/v7_crypto_universe.json").read_text())


def test_crypto_only_hot_warm_budgets_pass_cutover_contract() -> None:
    value = config()
    assert set(value["resource_budget"]) == {"hot", "warm"}
    validate_crypto_universe_resource_budget(value)


def test_retired_structural_budget_is_rejected() -> None:
    value = config()
    value["resource_budget"]["structural"] = {"market_capacity": 1}
    with pytest.raises(SystemExit, match="retired structural universe budget is forbidden"):
        validate_crypto_universe_resource_budget(value)


def test_missing_or_nonpositive_operational_budget_is_rejected() -> None:
    missing = config()
    missing["resource_budget"].pop("warm")
    with pytest.raises(SystemExit, match="warm resource budget missing"):
        validate_crypto_universe_resource_budget(missing)

    invalid = copy.deepcopy(config())
    invalid["resource_budget"]["hot"]["cpu_budget_micros_per_second"] = 0
    with pytest.raises(SystemExit, match="cpu_budget_micros_per_second.*positive"):
        validate_crypto_universe_resource_budget(invalid)
