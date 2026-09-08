from pathlib import Path
import json, unittest
ROOT=Path(__file__).resolve().parents[1]
class LiveScopeTests(unittest.TestCase):
    def test_current_research_paper_scope_has_two_engines_and_one_execution_owner(self):
        s=json.loads((ROOT/"config/v7_live_model_scope.json").read_text())
        self.assertEqual(set(s["live_algorithms"]),{"CRYPTO_SETTLEMENT_ENGINE","STRUCTURAL_ARB_ENGINE"})
        self.assertEqual(s["live_algorithm_count"],2);self.assertTrue(s["paper_only"]);self.assertFalse(s["real_order_submission"])
        inv=s["runtime_invariants"];self.assertTrue(inv["single_execution_owner"]);self.assertEqual(inv["global_portfolio_coordinator"],"V7_GLOBAL_PORTFOLIO_COORDINATOR")
        self.assertNotIn("legacy_algorithm_families_removed",s);self.assertNotIn("governance",s)
if __name__=="__main__":unittest.main()
