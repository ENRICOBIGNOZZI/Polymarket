from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROGRAM = r'''
#include "pm/v7_external_ws.hpp"
using namespace pm::v7::external_fair;
static_assert(external_ws_clock_policy_valid(true, true, true));
static_assert(external_ws_clock_policy_valid(true, false, false));
static_assert(external_ws_clock_policy_valid(false, false, false));
static_assert(!external_ws_clock_policy_valid(false, true, false));
static_assert(!external_ws_clock_policy_valid(false, false, true));
static_assert(!external_ws_clock_policy_valid(false, true, true));
int main() {
    ExternalVenueConnectionSpec spec;
    if (spec.capture_wall_time != 1) return 1;
    spec.capture_wall_time = 0;
    if (!external_ws_clock_policy_valid(false, false, false)) return 2;
    if (external_ws_clock_policy_valid(false, true, false)) return 3;
    if (external_ws_clock_policy_valid(false, false, true)) return 4;
    return 0;
}
'''


def test_external_ws_monotonic_only_policy() -> None:
    compiler = shutil.which("c++")
    assert compiler
    with tempfile.TemporaryDirectory() as tmp:
        binary = Path(tmp) / "external-ws-clock-policy"
        subprocess.run(
            [compiler, "-std=c++20", "-O2", "-Wall", "-Wextra", "-Wpedantic",
             f"-I{ROOT / 'include'}", "-x", "c++", "-", "-o", str(binary)],
            input=PROGRAM, text=True, check=True, capture_output=True,
        )
        subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    test_external_ws_monotonic_only_policy()
