from __future__ import annotations

import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_clob_pre_wire_benchmark_smoke() -> None:
    compiler = shutil.which("c++")
    pkg_config = shutil.which("pkg-config")
    assert compiler and pkg_config
    flags = subprocess.run(
        [pkg_config, "--cflags", "--libs", "openssl"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    sources = [
        "src/v7_clob_order_amounts.cpp",
        "src/v7_clob_order_salt.cpp",
        "src/v7_clob_eip712.cpp",
        "src/v7_keccak_fast.cpp",
        "src/v7_clob_wire.cpp",
        "src/v7_clob_http_frame.cpp",
        "tools/v7_clob_pre_wire_bench.cpp",
    ]
    with tempfile.TemporaryDirectory() as tmp:
        binary = Path(tmp) / "pre-wire-bench"
        command = [
            compiler, "-std=c++20", "-O2", "-Wall", "-Wextra", "-Wpedantic",
            f"-I{ROOT / 'include'}",
            *[str(ROOT / source) for source in sources],
            *shlex.split(flags), "-o", str(binary),
        ]
        subprocess.run(command, check=True, capture_output=True, text=True)
        result = subprocess.run(
            [str(binary), "64"], check=True, capture_output=True, text=True, timeout=20,
        )

    output = result.stdout
    for label in (
        "amount_salt_decimal", "eip712", "json_body", "l2_hmac",
        "http_frame", "total_excluding_secp256k1_and_socket",
    ):
        assert f"{label} p50_ns=" in output
        assert " p95_ns=" in output
        assert " p99_ns=" in output
    assert "samples=64" in output
