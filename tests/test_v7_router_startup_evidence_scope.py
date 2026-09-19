from __future__ import annotations

import gzip
import importlib.util
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "v7_external_fair_research", ROOT / "scripts/v7_external_fair_research.py"
)
assert SPEC and SPEC.loader
router = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = router
SPEC.loader.exec_module(router)


def test_native_slow_context_does_not_scan_historical_journals() -> None:
    from v7_slow_context import publish_contexts, context_path
    import json
    with tempfile.TemporaryDirectory() as directory:
        run_root = Path(directory)
        durable = run_root / "paper_v7_durable" / "external_fair"
        durable.mkdir(parents=True)
        sealed = durable / "counterfactuals.jsonl.segment-00000000000000000001.jsonl.gz"
        sealed.write_bytes(b"not-a-gzip")
        count = publish_contexts(run_root, code_sha="a" * 40, run_id="r",
            markets={"BTC:M5":{"asset":"BTC","horizon":"M5","market_id":"m"}})
        assert count == 1
        value = json.loads(context_path(run_root,"m").read_text())
        assert all(field is None for field in value["fields"].values())
        assert sealed.read_bytes() == b"not-a-gzip"


def test_recovery_paths_are_live_only_but_writes_still_target_both_journals() -> None:
    with tempfile.TemporaryDirectory() as directory:
        run_root = Path(directory) / "paper_v7_live"
        expected = [run_root / "external_fair" / "counterfactuals.jsonl"]
        assert router._paper_exploration_recovery_paths(run_root) == expected
        write_paths = router._paper_exploration_evidence_paths(run_root)
        assert expected[0] in write_paths
        assert run_root.parent / "paper_v7_durable" / "external_fair" / "counterfactuals.jsonl" in write_paths
        assert len(write_paths) == 2


if __name__ == "__main__":
    test_native_slow_context_does_not_scan_historical_journals()
    test_recovery_paths_are_live_only_but_writes_still_target_both_journals()
