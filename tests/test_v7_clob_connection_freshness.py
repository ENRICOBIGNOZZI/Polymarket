from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = r'''
#include "pm/v7_clob_connection_freshness.hpp"
using pm::v7::clob::ConnectionFreshnessGate;
int main() {
    ConnectionFreshnessGate g(1'000);
    if (!g.valid() || g.ready(1)) return 1;
    g.on_connect(100);
    if (g.epoch() != 1 || g.ready(100) || !g.heartbeat_due(100, 200)) return 2;
    if (!g.record_roundtrip(150) || !g.ready(150) || !g.ready(1'150)) return 3;
    if (g.ready(1'151)) return 4;
    if (g.heartbeat_due(949, 200) || !g.heartbeat_due(950, 200)) return 5;
    if (g.record_roundtrip(149)) return 6;
    g.on_disconnect();
    if (g.ready(200) || g.last_roundtrip_ns() != 0) return 7;
    g.on_connect(300);
    if (g.epoch() != 2 || g.record_roundtrip(299)) return 8;
    if (!g.record_roundtrip(301) || !g.ready(301)) return 9;
    ConnectionFreshnessGate bad(0);
    if (bad.valid() || bad.ready(1)) return 10;
    return 0;
}
'''

def test_connection_freshness_gate() -> None:
    compiler = shutil.which("c++")
    assert compiler
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp)
        source = p / "main.cpp"
        binary = p / "freshness-test"
        source.write_text(PROGRAM)
        subprocess.run([
            compiler, "-std=c++20", "-O2", "-Wall", "-Wextra", "-Wpedantic",
            f"-I{ROOT / 'include'}", str(source), "-o", str(binary),
        ], check=True, capture_output=True, text=True)
        subprocess.run([str(binary)], check=True)
