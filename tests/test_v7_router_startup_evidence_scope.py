from __future__ import annotations

import gzip
import importlib.util
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "v7_external_fair_paper_router", ROOT / "scripts/v7_external_fair_paper_router.py"
)
assert SPEC and SPEC.loader
router = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = router
SPEC.loader.exec_module(router)


def test_live_startup_and_maturity_ignore_cross_sha_durable_history() -> None:
    with tempfile.TemporaryDirectory() as directory:
        run_root = Path(directory)
        durable = run_root / "paper_v7_durable" / "external_fair"
        durable.mkdir(parents=True)
        # A deliberately invalid historical gzip proves the live critical path
        # never opens durable cross-SHA history. Explicit maintenance would fail.
        sealed = durable / "counterfactuals.jsonl.segment-00000000000000000001.jsonl.gz"
        sealed.write_bytes(b"not-a-gzip")
        instance = router.PaperRouter(
            run_root, "a" * 40, ROOT / "config" / "v7_external_fair.json",
            "https://clob.invalid", "https://gamma.invalid",
        )
        diagnostics = instance.maturity_diagnostics()
        assert diagnostics["forecast_rows"] == 0
        assert diagnostics["research_evidence_sufficient"] is False
        try:
            instance.compact_durable_evidence()
        except (OSError, EOFError, gzip.BadGzipFile, RuntimeError):
            pass
        else:
            raise AssertionError("explicit durable maintenance unexpectedly accepted corrupt history")


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
    test_live_startup_and_maturity_ignore_cross_sha_durable_history()
    test_recovery_paths_are_live_only_but_writes_still_target_both_journals()
