from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _compiler() -> str:
    compiler = shutil.which("c++")
    assert compiler, "C++ compiler not found"
    return compiler


def _openssl_flags() -> tuple[list[str], list[str]]:
    pkg = shutil.which("pkg-config")
    if pkg:
        cflags = subprocess.check_output([pkg, "--cflags", "openssl"], text=True).split()
        libs = subprocess.check_output([pkg, "--libs", "openssl"], text=True).split()
        return cflags, libs
    brew = shutil.which("brew")
    assert brew, "neither pkg-config nor brew available for OpenSSL"
    prefix = subprocess.check_output([brew, "--prefix", "openssl@3"], text=True).strip()
    return [f"-I{prefix}/include"], [f"-L{prefix}/lib", "-lssl", "-lcrypto"]


def test_native_clob_rate_limiter() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        binary = Path(tmp) / "rate-limit-test"
        subprocess.run(
            [
                _compiler(), "-std=c++20", "-O2", "-Wall", "-Wextra", "-Wpedantic",
                f"-I{ROOT / 'include'}",
                str(ROOT / "src/v7_clob_rate_limiter.cpp"),
                str(ROOT / "tests/test_v7_clob_rate_limiter.cpp"),
                "-o", str(binary),
            ],
            check=True, capture_output=True, text=True,
        )
        subprocess.run([str(binary)], check=True, timeout=10)


def test_native_user_ws_parser_and_transport_compile() -> None:
    cflags, libs = _openssl_flags()
    with tempfile.TemporaryDirectory() as tmp:
        binary = Path(tmp) / "user-ws-test"
        subprocess.run(
            [
                _compiler(), "-std=c++20", "-O2", "-Wall", "-Wextra", "-Wpedantic",
                f"-I{ROOT / 'include'}", *cflags,
                str(ROOT / "src/boost_json.cpp"),
                str(ROOT / "src/v7_user_ws.cpp"),
                str(ROOT / "src/v7_user_ws_feed.cpp"),
                str(ROOT / "tests/test_v7_user_ws.cpp"),
                "-o", str(binary), *libs, "-lpthread",
            ],
            check=True, capture_output=True, text=True,
        )
        subprocess.run([str(binary)], check=True, timeout=10)


def test_native_hot_tls_lanes_compile() -> None:
    cflags, libs = _openssl_flags()
    program = r'''
#include "pm/v7_clob_lanes.hpp"
int main() {
    pm::v7::clob::HotConnectionLanes lanes("localhost", 443, 10);
    if (lanes.ready()) return 2;
    lanes.close();
    return lanes.ready() ? 3 : 0;
}
'''
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        main = tmp_path / "main.cpp"
        binary = tmp_path / "hot-lanes-test"
        main.write_text(program)
        subprocess.run(
            [
                _compiler(), "-std=c++20", "-O2", "-Wall", "-Wextra", "-Wpedantic",
                f"-I{ROOT / 'include'}", *cflags,
                str(ROOT / "src/v7_clob_tls.cpp"),
                str(ROOT / "src/v7_clob_lanes.cpp"),
                str(main), "-o", str(binary), *libs,
            ],
            check=True, capture_output=True, text=True,
        )
        subprocess.run([str(binary)], check=True, timeout=10)
