import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v7_structural_relations as s


def relation(method="DETERMINISTIC_RULE"):
    payload = {"event": "e", "rules": "r"}
    return s.StructuralRelation(
        "r1", 1, s.RelationType.COMPLETE_SET, ("m1",), ("yes", "no"),
        ((1.0, 0.0), (0.0, 1.0)), 1.0, "official", "rules", s.semantic_hash(payload),
        method, True, True,
    )


def test_registry_precompiles_instrument_to_relation_index():
    registry = s.StructuralRegistry([relation()])
    assert registry.affected("yes")[0].relation_id == "r1"
    assert registry.affected("missing") == ()


def test_llm_cannot_certify_structural_relation():
    try:
        relation("LLM").validate()
    except s.StructuralError as exc:
        assert str(exc) == "llm_cannot_certify_relation"
    else:
        raise AssertionError("LLM-certified relation passed")


def test_full_depth_profit_and_insufficient_depth_fail_closed():
    row = relation()
    books = {
        "yes": [s.BookLevel(.45, 10)],
        "no": [s.BookLevel(.45, 10)],
    }
    plan = s.plan_full_depth(row, 5, books, {"yes": 0.0, "no": 0.0})
    assert plan.executable and abs(plan.net_profit_floor - .5) < 1e-12
    blocked = s.plan_full_depth(row, 11, books, {"yes": 0.0, "no": 0.0})
    assert not blocked.executable and blocked.blocker == "insufficient_full_depth"


def test_sequential_order_uses_direct_joint_states():
    pnl = {"NONE": 0.0, "L1": -1.0, "L2": -.2, "FULL": 2.0}
    distributions = {
        ("a", "b"): {"NONE": .1, "L1": .3, "L2": .1, "FULL": .5},
        ("b", "a"): {"NONE": .1, "L1": .1, "L2": .1, "FULL": .7},
    }
    assert s.sequential_order_ev(distributions, pnl) == ("b", "a")
