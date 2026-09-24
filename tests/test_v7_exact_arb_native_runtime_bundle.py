"""The bounded native projection must not weaken proof/source contracts."""
from copy import deepcopy
import hashlib
import json

import pytest

from test_v7_exact_arb_hotset_selection import MODEL, _event, _raw_market, _registry, _hotset
from v7_exact_arb_exchange_universe import build_snapshot
from v7_exact_arb_hotset_selection import compile_selection
from v7_exact_arb_native_compile import compile_runtime_bundle
from v7_unified_exact_arb_graph import GraphError, compile_graph, generation_hash


def fixture():
    universe = build_snapshot([_event([_raw_market("1")])], MODEL, generated_at_ms=100)
    graph = compile_graph([_registry()], universe, MODEL)
    return universe, graph


def bundle(graph):
    return compile_runtime_bundle(graph, MODEL, [graph["relations"][0]["relation_id"]], ["1001", "2001"])


def test_native_bundle_is_deterministic_proof_carrying_and_zero_authority():
    _, graph = fixture()
    wire = bundle(graph)
    assert bundle(deepcopy(graph)) == wire
    assert hashlib.sha256(wire["payload"].encode()).hexdigest() == wire["sha256"]
    body = json.loads(wire["payload"])
    assert body["paper_only"] is True
    for key in ("execution_authority", "authenticated_execution", "real_order_submission",
                "real_capital_at_risk", "automatic_promotion"):
        assert body[key] is False
    assert len(body["nodes"]) == len(body["relations"]) == 2
    assert body["dependencies"] == [[0, 1], [0, 1]]
    assert body["relations"][0]["sell_inventory"] is False
    assert body["relations"][1]["sell_inventory"] is True
    assert body["relations"][0]["legs"][0]["coefficient"] == ["1", "1"]
    assert body["excluded"] == []


def test_hotset_publishes_membership_lease_and_native_operands_atomically():
    universe, graph = fixture()
    hotset = {**_hotset(graph, [graph["relations"][0]["relation_id"]]), "timestamp_ms": 101}
    selection, _ = compile_selection(graph, universe, hotset, MODEL, 64, as_of_ms=102)
    native = json.loads(selection["native_runtime_bundle"]["payload"])
    assert native["graph_generation"] == selection["graph_generation"]
    assert native["model_sha"] == selection["model_sha"]
    assert selection["valid_until_ms"] == 120101


def test_full_exchange_node_count_does_not_expand_native_runtime_bounds():
    _, graph = fixture()
    template = graph["nodes"][0]
    graph["nodes"] += [{**template, "token_id": f"unselected-{i}", "node_id": format(i+999999, "064x")}
                       for i in range(65537)]
    graph["graph_generation"] = generation_hash(graph)
    body = json.loads(bundle(graph)["payload"])
    assert len(body["nodes"]) == 2
    assert len(body["relations"]) == 2


@pytest.mark.parametrize("mutation,reason", [
    (lambda r: r["legs"][0].update(payout_vector=["0", "0"]), "non-constant basket payout"),
    (lambda r: r["legs"][0].update(node_id="0" * 64), "native_claim_identity"),
    (lambda r: r["legs"][0].update(fee_rate=None), "rational"),
    (lambda r: r["legs"][0].update(fee_rounding_mode="EXACT"), "native_fee_rounding_unsupported"),
    (lambda r: r.update(transformation={"kind": "MERGE"}), "native_transform_stage_requires_runtime_resource_evidence"),
])
def test_recomputed_checksum_does_not_make_invalid_operands_executable(mutation, reason):
    _, graph = fixture()
    mutation(graph["relations"][0])
    graph["graph_generation"] = generation_hash(graph)
    body = json.loads(bundle(graph)["payload"])
    assert body["relations"] == []
    assert reason in body["excluded"][0]["reason"]


def test_missing_required_leg_is_excluded_not_partially_lowered():
    _, graph = fixture()
    result = compile_runtime_bundle(graph, MODEL, [graph["relations"][0]["relation_id"]], ["1001"])
    body = json.loads(result["payload"])
    assert body["relations"] == []
    assert body["dependencies"] == [[]]
    assert len(body["excluded"]) == 1


def test_explicit_native_bounds_and_duplicate_selection_rejected():
    _, graph = fixture()
    rid = graph["relations"][0]["relation_id"]
    with pytest.raises(GraphError, match="token_capacity"):
        compile_runtime_bundle(graph, MODEL, [rid], [str(i) for i in range(129)])
    with pytest.raises(GraphError, match="collision"):
        compile_runtime_bundle(graph, MODEL, [rid], ["1001", "1001"])
    with pytest.raises(GraphError, match="collision"):
        compile_runtime_bundle(graph, MODEL, [rid, rid], ["1001", "2001"])
    with pytest.raises(GraphError, match="relation_identity"):
        compile_runtime_bundle(graph, MODEL, ["unknown"], ["1001", "2001"])
