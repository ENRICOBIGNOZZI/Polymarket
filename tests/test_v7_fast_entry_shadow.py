import importlib.util
import pathlib
import sys
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "v7_fast_entry_shadow", ROOT / "scripts" / "v7_fast_entry_shadow.py"
)
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MOD)

SHA = "a" * 40


class FastEntryShadowTest(unittest.TestCase):
    def valid_status(self):
        return {
            "code_sha": SHA,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "market": {
                "market_id": "m1",
                "event_id": "e1",
                "yes_token": "yes",
                "no_token": "no",
            },
            "fair": {"tte_seconds": 42.0},
        }

    def books(self):
        yes = MOD.Book("yes", ((0.49, 10.0),), ((0.51, 10.0),), 0.01, 1.0, 1000, 1000, "y")
        no = MOD.Book("no", ((0.49, 10.0),), ((0.51, 10.0),), 0.01, 1.0, 1000, 1000, "n")
        return {"yes": yes, "no": no}

    def test_invalid_identity_fails_closed_without_book_request(self):
        status = self.valid_status()
        status["code_sha"] = "b" * 40
        with patch.object(MOD, "_books") as books:
            row = MOD.observe_once(status, {}, model_sha=SHA, clob_url="https://example.invalid")
        books.assert_not_called()
        self.assertFalse(row["arrival_revalidated"])
        self.assertEqual(row["candidate_count"], 0)
        self.assertEqual(row["reason"], "STATUS_IDENTITY_OR_SAFETY_INVALID")
        self.assertEqual(row["execution_authority"], "ZERO_AUTHORITY_RESEARCH_ONLY")
        self.assertFalse(row["real_order_submission"])

    def test_fresh_batch_is_revalidated_without_synthetic_delay(self):
        candidate = {"outcome": "YES", "robust_ev": 0.012}
        with patch.object(MOD, "_books", return_value=(self.books(), 17, "")), \
             patch.object(MOD, "robust_candidates", return_value=[candidate]):
            row = MOD.observe_once(
                self.valid_status(), {}, model_sha=SHA, clob_url="https://example.invalid"
            )
        self.assertTrue(row["arrival_revalidated"])
        self.assertEqual(row["fresh_book_count"], 2)
        self.assertEqual(row["decision_to_fresh_book_ms"], 17)
        self.assertEqual(row["best_outcome"], "YES")
        self.assertAlmostEqual(row["best_robust_ev_per_share"], 0.012)
        self.assertEqual(row["reason"], "ROBUST_CANDIDATE")
        self.assertEqual(row["execution_authority"], "ZERO_AUTHORITY_RESEARCH_ONLY")

    def test_latency_only_never_computes_or_records_economic_candidates(self):
        with patch.object(MOD, "_books", return_value=(self.books(), 19, "")):
            with patch.object(MOD, "robust_candidates") as candidates:
                row = MOD.observe_once(
                    self.valid_status(), {}, model_sha=SHA,
                    clob_url="https://example.invalid", latency_only=True,
                )
        candidates.assert_not_called()
        self.assertEqual(row["measurement_mode"], "LATENCY_ONLY_NO_ECONOMIC_ENDPOINTS")
        self.assertEqual(row["decision_to_fresh_book_ms"], 19)
        self.assertEqual(row["book_exchange_timestamp_span_ms"], 0)
        self.assertEqual(row["oldest_book_age_at_receive_ms"], 0)
        self.assertTrue(row["arrival_revalidated"])
        self.assertNotIn("candidate_count", row)
        self.assertNotIn("best_outcome", row)
        self.assertNotIn("best_robust_ev_per_share", row)
        self.assertNotIn("market_yes", row)
        self.assertNotIn("tte_seconds", row)

    def test_latency_only_labels_complement_incoherence_fail_closed(self):
        with patch.object(MOD, "_books", return_value=(self.books(), 21, "")):
            with patch.object(MOD, "live_market_yes", return_value=None):
                with patch.object(MOD, "robust_candidates") as candidates:
                    row = MOD.observe_once(
                        self.valid_status(), {}, model_sha=SHA,
                        clob_url="https://example.invalid", latency_only=True,
                    )
        candidates.assert_not_called()
        self.assertFalse(row["arrival_revalidated"])
        self.assertEqual(row["fresh_book_count"], 2)
        self.assertEqual(row["reason"], "BOOK_BATCH_RECEIVED_BUT_COMPLEMENT_INCOHERENT")

    def test_module_has_no_order_or_ledger_writer_dependency(self):
        source = (ROOT / "scripts" / "v7_fast_entry_shadow.py").read_text(encoding="utf-8")
        self.assertNotIn("spool_event", source)
        self.assertNotIn("opportunities/inbox", source)
        self.assertNotIn("canonical_ledger", source)
        self.assertIn("synthetic_revalidation_sleep_ms", source)
        self.assertIn("default=250", source)


if __name__ == "__main__":
    unittest.main()
