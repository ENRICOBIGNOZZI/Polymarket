from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v7_slow_fast_probability_diagnostic as m


def test_slow_vector_preserves_missingness_and_uses_train_stats_only():
    stats = {name: (10.0, 2.0) for name in m.SLOW_FIELDS}
    row = {"slow_context": {"values": {name: None for name in m.SLOW_FIELDS}}}
    row["slow_context"]["values"]["return_5s"] = 14.0
    z = m.slow_vector(row, stats)
    j = m.SLOW_FIELDS.index("return_5s") * 2
    assert z[j] == 2.0
    assert z[j + 1] == 1.0
    k = m.SLOW_FIELDS.index("oracle_value") * 2
    assert z[k] == 0.0
    assert z[k + 1] == 0.0


def test_coverage_does_not_turn_missing_fields_into_zero_observations():
    values = {name: None for name in m.SLOW_FIELDS}
    rows = [{"slow_context": {"values": dict(values)}} for _ in range(2)]
    rows[0]["slow_context"]["values"]["return_5s"] = 0.0
    cov = m.coverage(rows)
    assert cov["return_5s"] == 0.5
    assert cov["oracle_value"] == 0.0
