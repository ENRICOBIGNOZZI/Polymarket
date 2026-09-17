from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROGRAM = r'''
#include "pm/v7_user_ws_feed.hpp"
#include <cassert>
#include <string>
#include <vector>
using namespace pm::v7::user_ws;
int main() {
    bool rejected = false;
    try {
        AuthenticatedFeed invalid({"", "", ""}, {}, [](const Event&){});
    } catch (...) { rejected = true; }
    assert(rejected);

    int events = 0;
    AuthenticatedFeed feed(
        {"key-test", "secret-test", "pass-test"},
        std::vector<std::string>{"0xcondition"},
        [&](const Event&) { ++events; });
    const auto snapshot = feed.snapshot();
    assert(!snapshot.connected);
    assert(snapshot.frames == 0);
    assert(snapshot.decoded_events == 0);
    assert(snapshot.invalid_frames == 0);
    assert(snapshot.reconnects == 0);
    assert(snapshot.transport_errors == 0);
    assert(events == 0);
    feed.stop();
    return 0;
}
'''


def _openssl_flags() -> tuple[list[str], list[str]]:
    pkg = shutil.which("pkg-config")
    if pkg:
        return (
            subprocess.check_output([pkg, "--cflags", "openssl"], text=True).split(),
            subprocess.check_output([pkg, "--libs", "openssl"], text=True).split(),
        )
    brew = shutil.which("brew")
    assert brew, "OpenSSL development flags unavailable"
    prefix = subprocess.check_output([brew, "--prefix", "openssl@3"], text=True).strip()
    return [f"-I{prefix}/include"], [f"-L{prefix}/lib", "-lssl", "-lcrypto"]


def test_user_ws_transport_compiles_and_is_cold_until_start() -> None:
    cxx = shutil.which("c++")
    assert cxx
    cflags, libs = _openssl_flags()
    with tempfile.TemporaryDirectory() as td:
        path = Path(td)
        source = path / "main.cpp"
        binary = path / "user-feed-test"
        source.write_text(PROGRAM)
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
                *cflags,
                str(ROOT / "src/v7_user_ws.cpp"),
                str(ROOT / "src/v7_user_ws_feed.cpp"),
                str(ROOT / "src/boost_json.cpp"),
                str(source),
                "-o",
                str(binary),
                *libs,
                "-lpthread",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run([str(binary)], check=True, timeout=10)
