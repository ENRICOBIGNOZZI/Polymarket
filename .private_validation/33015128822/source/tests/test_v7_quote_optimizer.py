#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_v7_quote_optimizer_never_uses_product_for_admission():
    text = (ROOT / "scripts/v7_bundle_quote_optimizer.py").read_text(encoding="utf-8")
    assert "math.prod" not in text
    assert "product_of_marginals_forbidden" in text
    assert "joint_completion_owner" in text
    assert '"joint_completion_owner": "v7_graph_roundtrip_guard.py"' in text
    assert "v7_graph_roundtrip_guard_empirical_fixed_horizon_joint_state" in text


def test_runtime_graph_path_uses_v3_roundtrip_guard():
    text = (ROOT / "scripts/paper_v7_execution_loop.sh").read_text(encoding="utf-8")
    assert "v7_graph_roundtrip_guard.py" in text
    assert "graph_roundtrip_state.json" in text
    graph_block = text[text.index("run_graph(){"):text.index("reap_stale_proxy")]
    assert "v7_graph_forward_guard.py" not in graph_block
    assert "v7_graph_execution_guard.py" not in graph_block
    assert "v6_bundle_state_guard.py" not in graph_block


if __name__ == "__main__":
    test_v7_quote_optimizer_never_uses_product_for_admission()
    test_runtime_graph_path_uses_v3_roundtrip_guard()
    print("ok 2 v7 quote/graph v3 runtime tests")
