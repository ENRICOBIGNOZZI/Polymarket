from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_clob_order_identity_compiles_and_runs() -> None:
    cxx = shutil.which("c++")
    assert cxx
    with tempfile.TemporaryDirectory() as td:
        binary = Path(td) / "order-identity-test"
        subprocess.run(
            [
                cxx,
                "-std=c++20",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Wpedantic",
                f"-I{ROOT / 'include'}",
                "-I/opt/homebrew/include",
                str(ROOT / "src/boost_json.cpp"),
                str(ROOT / "src/v7_clob_order_identity.cpp"),
                str(ROOT / "tests/test_v7_clob_order_identity.cpp"),
                "-o",
                str(binary),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run([str(binary)], check=True, timeout=10)
