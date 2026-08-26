from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.lf_v6_pair_factor_basis_audit import (  # noqa: E402
    deterministic_fixture,
    run_audit,
    shared_pair_factor_loadings,
)


class PairFactorBasisAuditTest(unittest.TestCase):
    def test_target_specific_loo_can_change_pair_loading_relation(self) -> None:
        result = run_audit()
        self.assertLess(
            result["target_specific_loo_loading_a"] * result["target_specific_loo_loading_b"],
            0.0,
        )
        self.assertGreater(
            result["shared_leave_pair_out_loading_a"] * result["shared_leave_pair_out_loading_b"],
            0.0,
        )
        self.assertTrue(result["pair_factor_sign_relation_changes"])
        self.assertGreater(result["ratio_distortion"], 8.0)

    def test_pair_factor_requires_independent_controls(self) -> None:
        series = deterministic_fixture()
        only_three = {key: series[key] for key in ("A", "B", "C")}
        with self.assertRaisesRegex(ValueError, "at least two controls"):
            shared_pair_factor_loadings(only_three, "A", "B")


if __name__ == "__main__":
    unittest.main()
