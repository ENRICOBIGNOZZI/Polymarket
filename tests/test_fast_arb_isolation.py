from pathlib import Path


def test_fast_arb_native_lane_has_no_statistical_model_dependency():
    source = Path("src/fast_arb.cpp").read_text(encoding="utf-8")
    header = Path("include/pm/fast_arb.hpp").read_text(encoding="utf-8")
    combined = (source + "\n" + header).lower()
    forbidden = (
        "direct_action",
        "dynamic_exit",
        "walk_forward",
        "python.h",
        "pybind",
        "torch",
        "sklearn",
    )
    for token in forbidden:
        assert token not in combined
    assert '#include "pm/fast_arb.hpp"' in source
