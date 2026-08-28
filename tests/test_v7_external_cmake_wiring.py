#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    cmake = (ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
    required_sources = (
        "src/v7_external_fair.cpp",
        "src/v7_external_state.cpp",
        "src/v7_external_execution.cpp",
        "src/v7_external_kernel.cpp",
        "src/v7_external_protocol.cpp",
        "src/v7_external_ingress.cpp",
        "src/v7_external_tape.cpp",
        "src/v7_external_replay.cpp",
    )
    for source in required_sources:
        assert source in cmake, f"missing pm_v7_external source: {source}"

    required_tests = (
        "pm_v7_external_fair_tests",
        "pm_v7_external_state_tests",
        "pm_v7_external_execution_tests",
        "pm_v7_external_kernel_tests",
        "pm_v7_external_protocol_tests",
        "pm_v7_external_ingress_tests",
        "pm_v7_external_tape_tests",
        "pm_v7_external_replay_tests",
    )
    for test in required_tests:
        assert test in cmake, f"missing CTest target: {test}"

    # Python V7 tests must remain auto-discovered rather than introducing a
    # second test runner/policy path for the external-fair plane.
    assert '"${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_*.py"' in cmake
    assert '"${CMAKE_CURRENT_SOURCE_DIR}/tests/test_monitoring_v7_*.py"' in cmake
    assert "-m pytest --quiet" in cmake
    assert "V7 Python tests require pytest" in cmake


if __name__ == "__main__":
    main()
