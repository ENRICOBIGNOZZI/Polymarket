from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_artifact_hash_matches_persisted_maker_identity():
    runtime = load("v7_runtime_artifacts_hash_test", ROOT / "scripts/v7_runtime_artifacts.py")
    maker = load("v7_maker_durable_learning_hash_test", ROOT / "scripts/v7_maker_durable_learning.py")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "identity.json"
        path.write_bytes(b"abc")
        assert maker.fnv1a64(path) == "e16801510db89efd"
        assert runtime.fnv1a64(path) == maker.fnv1a64(path)


def test_cpp_and_python_persisted_fnv_offset_remain_aligned():
    expected = "1469598103934665603"
    for relative in (
        "scripts/v7_runtime_artifacts.py",
        "scripts/v7_maker_durable_learning.py",
        "include/pm/v7_external_replay.hpp",
        "src/v7_maker_markout_observer.cpp",
        "src/v7_authorized_maker_paper_executor.cpp",
    ):
        assert expected in (ROOT / relative).read_text(encoding="utf-8")
