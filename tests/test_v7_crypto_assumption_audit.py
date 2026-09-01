from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_crypto_assumption_audit import audit  # noqa: E402


def test_every_btc_occurrence_is_classified_and_old_engine_is_not_operational() -> None:
    result = audit(ROOT)
    assert result["occurrence_count"] > 0
    assert result["passed"] is True
    assert result["operational_stale_count"] == 0
    assert sum(result["counts"].values()) == result["occurrence_count"]
    assert all(row["category"] in {
        "A_BTC_SPECIFIC", "B_CRYPTO_GENERIC_HARDCODED", "C_FIXTURE_TEST",
        "D_DOCUMENTATION", "E_STALE_LEGACY",
    } for row in result["occurrences"])


if __name__ == "__main__":
    test_every_btc_occurrence_is_classified_and_old_engine_is_not_operational()
