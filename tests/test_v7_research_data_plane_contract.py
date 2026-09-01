from __future__ import annotations

import copy
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_research_data_plane_contract import ResearchContractError, validate  # noqa: E402


def inputs() -> tuple[dict, dict, dict]:
    return tuple(
        json.loads((ROOT / path).read_text(encoding="utf-8"))
        for path in (
            "config/v7_research_data_plane.json",
            "config/v7_authority_registry.json",
            "config/v7_live_model_scope.json",
        )
    )


def test_all_retained_research_has_unique_purpose_and_zero_authority() -> None:
    contract, authority, scope = inputs()
    report = validate(ROOT, contract, authority, scope)
    assert report["gate_passed"] is True
    assert report["family_count"] == 10
    assert report["all_authorities_false"] is True
    assert report["trading_token_allowed"] is False


def test_any_reintroduced_research_authority_fails_closed() -> None:
    contract, authority, scope = inputs()
    broken = copy.deepcopy(contract)
    broken["families"]["micro_taker"]["authorities"]["orders"] = True
    try:
        validate(ROOT, broken, authority, scope)
    except ResearchContractError as exc:
        assert str(exc) == "authority:micro_taker"
    else:
        raise AssertionError("research order authority accepted")


def test_required_ablation_cannot_be_silently_removed() -> None:
    contract, authority, scope = inputs()
    broken = copy.deepcopy(contract)
    broken["families"]["graph_rv"]["ablations"].remove("mapping_uncertainty")
    try:
        validate(ROOT, broken, authority, scope)
    except ResearchContractError as exc:
        assert str(exc) == "ablations:graph_rv"
    else:
        raise AssertionError("incomplete research ablation set accepted")


if __name__ == "__main__":
    test_all_retained_research_has_unique_purpose_and_zero_authority()
    test_any_reintroduced_research_authority_fails_closed()
    test_required_ablation_cannot_be_silently_removed()
