from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROGRAM = r'''
#include "pm/v7_clob_order_lane_selector.hpp"
using namespace pm::v7::clob;
int main() {
    OrderLaneSelector bad0(0, 1000), bad5(5, 1000), badidle(1, 0);
    if (bad0.valid() || bad5.valid() || badidle.valid()) return 1;

    OrderLaneSelector s(3, 1000);
    if (!s.valid() || s.lane_count() != 3) return 2;
    if (!s.on_connected(0, 10, 100)
        || !s.on_connected(1, 20, 100)
        || !s.on_connected(2, 30, 100)) return 3;
    if (s.on_connected(1, 21, 101)) return 4;
    if (s.acquire(42, 120).valid) return 5;
    if (!s.record_roundtrip(0, 10, 150, 300)
        || !s.record_roundtrip(1, 20, 160, 100)
        || !s.record_roundtrip(2, 30, 170, 200)) return 6;

    const auto a = s.acquire(42, 200);
    if (!a.valid || a.lane != 1) return 7;
    if (s.acquire(42, 200).valid) return 8;
    auto forged = a; ++forged.lease_generation;
    if (s.mark_send_started(forged)) return 9;
    if (s.record_roundtrip(1, 20, 201, 50)) return 10;
    if (!s.mark_send_started(a)) return 11;
    if (!s.transport_failure(a)) return 12;
    const auto after_fail = s.snapshot(1);
    if (after_fail.connected || after_fail.phase != OrderLanePhase::SentAmbiguous
        || after_fail.active_order_id != 42) return 13;
    if (s.acquire(42, 210).valid) return 14;
    const auto b = s.acquire(43, 210);
    if (!b.valid || b.lane != 2) return 15;
    if (!s.cancel_unsent(b)) return 16;
    if (!s.resolve_ambiguous(a)) return 17;
    if (s.resolve_ambiguous(a)) return 18;

    if (!s.on_connected(1, 21, 250)
        || !s.record_roundtrip(1, 21, 270, 50)) return 19;
    const auto c = s.acquire(44, 300);
    if (!c.valid || c.lane != 1) return 20;
    if (!s.mark_send_started(c)) return 21;
    if (!s.complete_response(c, 320, 40)) return 22;
    const auto completed = s.snapshot(1);
    if (completed.phase != OrderLanePhase::Idle || completed.active_order_id != 0
        || completed.last_rtt_ns != 40) return 23;
    if (s.complete_response(c, 330, 30)) return 24;
    const auto d = s.acquire(45, 330);
    if (!d.valid || d.lane != 1) return 25;
    if (!s.on_disconnected(1, 21)) return 26;
    const auto reserved_drop = s.snapshot(1);
    if (reserved_drop.connected || reserved_drop.phase != OrderLanePhase::Idle
        || reserved_drop.active_order_id != 0) return 27;
    if (s.cancel_unsent(d)) return 28;

    if (!s.on_connected(1, 22, 350)
        || !s.record_roundtrip(1, 22, 360, 60)) return 29;
    const auto e = s.acquire(46, 370);
    if (!e.valid || e.lane != 1 || !s.mark_send_started(e)) return 30;
    if (!s.on_disconnected(1, 22)) return 31;
    const auto sent_drop = s.snapshot(1);
    if (sent_drop.connected || sent_drop.phase != OrderLanePhase::SentAmbiguous
        || sent_drop.active_order_id != 46) return 32;
    if (s.acquire(46, 380).valid) return 33;
    if (!s.resolve_ambiguous(e)) return 34;
    if (s.acquire(47, 1200).valid) return 35;

    if (!s.on_connected(1, 23, 1210)
        || !s.record_roundtrip(1, 23, 1220, 70)) return 36;
    const auto f = s.acquire(48, 1230);
    if (!f.valid || f.lane != 1) return 37;
    auto old_epoch = f; old_epoch.connection_epoch = 22;
    if (s.cancel_unsent(old_epoch)) return 38;
    if (!s.cancel_unsent(f)) return 39;
    return 0;
}
'''


def test_order_lane_selector_is_single_submit_and_ambiguity_safe() -> None:
    compiler = shutil.which("c++")
    assert compiler
    with tempfile.TemporaryDirectory() as tmp:
        binary = Path(tmp) / "lane-selector-test"
        subprocess.run([
            compiler, "-std=c++20", "-O2", "-Wall", "-Wextra", "-Wpedantic",
            f"-I{ROOT / 'include'}", str(ROOT / "src/v7_clob_order_lane_selector.cpp"),
            "-x", "c++", "-", "-o", str(binary),
        ], input=PROGRAM, text=True, check=True, capture_output=True)
        subprocess.run([str(binary)], check=True)
