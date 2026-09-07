from __future__ import annotations

import copy
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from test_v7_crypto_execution_alpha import packet  # noqa: E402
from test_v7_opportunity import envelope  # noqa: E402
from v7_opportunity import OpportunityEnvelope, OpportunityError  # noqa: E402


def make_with_packet() -> dict:
    value = envelope(
        action="MAKE", component="professional_maker", ev=0.5, key="make-alpha",
        authority="PAPER_EXPLORATION", research_only=False,
    )
    value["execution_alpha"] = packet("MAKE")
    return value


def test_execution_alpha_packet_is_part_of_canonical_opportunity() -> None:
    parsed = OpportunityEnvelope.parse(make_with_packet())
    assert parsed.action == "MAKE"
    assert parsed.raw["execution_alpha"]["fill_probability"]["point"] == 0.30


def test_execution_alpha_ev_must_match_canonical_conservative_wealth_change() -> None:
    value = make_with_packet()
    value["conservative_expected_wealth_change"] = 0.6
    try:
        OpportunityEnvelope.parse(value)
    except OpportunityError as exc:
        assert str(exc) == "execution_alpha_ev_mismatch"
    else:
        raise AssertionError("execution-alpha EV disagreement accepted")


def test_execution_alpha_feature_cut_cannot_look_into_the_future() -> None:
    value = make_with_packet()
    value["execution_alpha"]["feature_receive_timestamp_ns"] = 101
    try:
        OpportunityEnvelope.parse(value)
    except OpportunityError as exc:
        assert str(exc) == "execution_alpha:packet_feature_clock"
    else:
        raise AssertionError("future execution-alpha feature cut accepted")


def test_structural_engine_cannot_smuggle_crypto_execution_alpha() -> None:
    value = envelope(
        engine="STRUCTURAL_ARB_ENGINE", action="ARB", component="hard_arb",
        ev=0.5, key="arb",
    )
    value["execution_alpha"] = packet("MAKE")
    try:
        OpportunityEnvelope.parse(value)
    except OpportunityError as exc:
        assert str(exc) == "execution_alpha_action_or_engine"
    else:
        raise AssertionError("crypto execution alpha accepted on structural engine")


def test_immature_packet_cannot_authorize_nonexploration_new_risk() -> None:
    value = make_with_packet()
    value["crypto_context"]["authority"] = "PAPER"
    try:
        OpportunityEnvelope.parse(value)
    except OpportunityError as exc:
        assert str(exc) == "execution_alpha_immature_new_risk"
    else:
        raise AssertionError("immature execution alpha accepted for ordinary new risk")


if __name__ == "__main__":
    test_execution_alpha_packet_is_part_of_canonical_opportunity()
    test_execution_alpha_ev_must_match_canonical_conservative_wealth_change()
    test_execution_alpha_feature_cut_cannot_look_into_the_future()
    test_structural_engine_cannot_smuggle_crypto_execution_alpha()
    test_immature_packet_cannot_authorize_nonexploration_new_risk()
