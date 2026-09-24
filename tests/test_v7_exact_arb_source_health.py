from pathlib import Path
from types import SimpleNamespace
import json
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from v7_exact_arb_source_health import universe_lease, graph_lease, lease
from v7_unified_exact_arb_graph import compile_graph, GraphError, SAFETY
from v7_unified_exact_arb_graph_shadow import Shadow


@pytest.mark.parametrize("stamp", [None, True, "100", -1, 101, 100 - 900001])
def test_invalid_or_expired_timestamp_is_rejected(stamp):
    with pytest.raises(ValueError):
        lease(stamp, 100, 900000, "source")


def test_explicit_invalid_source_cannot_compile():
    with pytest.raises(GraphError, match="invalid_universe_source"):
        compile_graph([], {**SAFETY, "model_sha": "a" * 40, "source_valid": False}, "a" * 40)


def test_lease_does_not_extend_from_compiler_heartbeat():
    graph = {"source_universe_valid": True, "source_universe_timestamp_ms": 100,
             "metadata": {"compiled_at_ms": 1000000}}
    with pytest.raises(ValueError):
        graph_lease(graph, 1000000)
    with pytest.raises(ValueError):
        universe_lease({"timestamp_ms": 100, "source_valid": False}, 100)


def test_cached_graph_still_expires_and_invalid_generation_blocks(tmp_path):
    model = "a" * 40
    graph = compile_graph([], {**SAFETY, "model_sha": model, "markets": [],
                              "source_valid": True, "timestamp_ms": 100}, model)
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(graph))
    shadow = Shadow(SimpleNamespace(model_sha=model, graph=path, opportunities=tmp_path / "opportunities.jsonl"))
    shadow.graph(as_of_ms=101)
    assert shadow.graph_state == "COLLECTING"
    shadow.graph(as_of_ms=900101)
    assert shadow.graph_state == "BLOCKED_INVALID_GRAPH"
    path.write_text(json.dumps({"state": "BLOCKED_SOURCE_INVALID"}))
    shadow.graph(as_of_ms=900102)
    assert shadow.graph_state == "BLOCKED_INVALID_GRAPH"
