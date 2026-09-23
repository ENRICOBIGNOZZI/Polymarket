"""Source-contract regressions. No network, account, or venue writes."""
from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("exchange_source_under_test", ROOT / "scripts/v7_exact_arb_exchange_universe.py")
assert SPEC is not None and SPEC.loader is not None
source = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(source)
MODEL = "a" * 40


def market(number=1, **overrides):
    row = dict(id=str(number), conditionId="0x" + format(number, "064x"),
               clobTokenIds=[str(number*2), str(number*2+1)], outcomes=["Yes", "No"],
               active=True, closed=False, acceptingOrders=True, enableOrderBook=True,
               feesEnabled=False, orderPriceMinTickSize="0.01", orderMinSize="5", negRisk=True)
    row.update(overrides)
    return row


def event(rows=None, **overrides):
    row = dict(id="event1", active=True, closed=False, negRisk=True,
               negRiskAugmented=False, markets=[market(1), market(2)] if rows is None else rows)
    row.update(overrides)
    return row


class SourceContractTests(unittest.TestCase):
    def test_standard_binary_mapping_keeps_provenance_not_onchain_claim(self):
        result = source.build_snapshot([event()], MODEL, generated_at_ms=123)
        self.assertEqual(result["timestamp_ms"], 123)
        self.assertEqual(len(result["markets"]), 2)
        for row in result["markets"]:
            self.assertTrue(row["binary_partition_verified"])
            self.assertIn("NOT_INDEPENDENT_ONCHAIN", row["partition_evidence_scope"])
            self.assertEqual(len(row["partition_proof_hash"]), 64)
        self.assertFalse(result["execution_authority"])
        self.assertTrue(all(result[k] is v for k, v in source.SAFETY.items()))

    def test_up_down_aliases_and_json_arrays_are_supported(self):
        row = market(outcomes='["Down", "Up"]', clobTokenIds='["2", "3"]')
        self.assertTrue(source._binary_partition(row)[0])

    def test_negrisk_payload_never_proves_complete_membership(self):
        result = source.build_snapshot([event()], MODEL)
        self.assertEqual(result["verified_negrisk_events"], 0)
        self.assertEqual(len(result["unverified_candidates"]), 1)
        self.assertEqual(result["unverified_candidates"][0]["reason"], "INDEPENDENT_COMPLETE_SET_ATTESTATION_REQUIRED")
        for row in result["markets"]:
            self.assertFalse(row["neg_risk_complete_set_verified"])
            self.assertEqual(row["neg_risk_complete_set_market_ids"], [])
            self.assertIsNone(row["neg_risk_complete_set_proof_hash"])

    def test_observed_subset_never_becomes_an_exhaustive_partition(self):
        full = [market(1), market(2), market(3)]
        for subset in (full, full[:2], full[1:]):
            with self.subTest(size=len(subset), first=subset[0]["id"]):
                result = source.build_snapshot([event(subset)], MODEL)
                self.assertFalse(any(r["neg_risk_complete_set_verified"] for r in result["markets"]))

    def test_missing_and_null_augmentation_flags_are_unknown(self):
        for flag in (None, 0, "false", "true"):
            with self.subTest(flag=flag):
                verified, _, reason = source._negrisk_attestation(event(negRiskAugmented=flag), [market(1), market(2)])
                self.assertFalse(verified)
                self.assertEqual(reason, "AUGMENTATION_METADATA_UNKNOWN")

    def test_augmented_membership_remains_unverified(self):
        result = source.build_snapshot([event(negRiskAugmented=True)], MODEL)
        self.assertEqual(result["augmented_negrisk_events"], 1)
        self.assertEqual(result["unverified_candidates"][0]["reason"], "AUGMENTED_NEGRISK_UNSTABLE_MEMBERSHIP")

    def test_direct_normalization_cannot_enable_unproven_relation(self):
        with self.assertRaisesRegex(ValueError, "cannot_attest"):
            source.normalize_market(market(), event(), negrisk_verified=True,
                                    negrisk_member_ids=["1", "2"], negrisk_reason="TRUST_ME")

    def test_unknown_fee_flag_is_not_free(self):
        for flag in (None, 0, 1, "false", "true", [], {}):
            with self.subTest(flag=flag):
                result = source.build_snapshot([event([market(feesEnabled=flag)])], MODEL)["markets"][0]
                self.assertFalse(result["fees_enabled_explicit"])
                self.assertIsNone(result["fees_enabled"])
                self.assertEqual(result["fee_schedule"], {})

    def test_explicit_disabled_fee_is_preserved(self):
        result = source.build_snapshot([event([market()])], MODEL)["markets"][0]
        self.assertTrue(result["fees_enabled_explicit"])
        self.assertIs(result["fees_enabled"], False)

    def test_unknown_nonzero_exponent_and_invalid_fee_never_default(self):
        for schedule in ({"rate": ".06"}, {"rate": "NaN", "exponent": 1},
                         {"rate": True, "exponent": 1}, {"rate": ".06", "exponent": -1},
                         {"rate": ".06", "exponent": "1/2"}, {"rate": "2", "exponent": 1}, []):
            with self.subTest(schedule=schedule):
                result = source._fee_terms(market(feesEnabled=True, feeSchedule=schedule))
                self.assertEqual(result[0], {})
                self.assertFalse(result[2])

    def test_conflicting_fee_schedule_cannot_fall_back_to_zero(self):
        schedule, flag, explicit, state = source._fee_terms(market(feesEnabled=False, feeSchedule={"rate": ".06", "exponent": 1}))
        self.assertEqual(schedule, {})
        self.assertIsNone(flag)
        self.assertFalse(explicit)
        self.assertEqual(state, "INVALID_OR_CONFLICTING_SCHEDULE")

    def test_valid_fee_terms_are_preserved(self):
        raw = market(feesEnabled=True, feeSchedule={"rate": ".06", "exponent": 1})
        terms, flag, explicit, _ = source._fee_terms(raw)
        self.assertEqual(terms["rate"], ".06")
        self.assertTrue(flag and explicit)

    def test_missing_tradability_never_becomes_open(self):
        for key in ("active", "closed", "acceptingOrders", "enableOrderBook"):
            for value in (None, "false", "true", 0, 1):
                row = market(**{key: value})
                with self.subTest(key=key, value=value):
                    self.assertEqual(source.build_snapshot([event([row])], MODEL)["markets"], [])

    def test_invalid_token_or_condition_never_attests_partition(self):
        for bad in (["2", None, "3"], ["2", "2"], ["02", "3"], [True, "3"],
                    ["2", str(2**256)], ["2", "3", "4"], ["2", "3.0"]):
            with self.subTest(tokens=bad):
                self.assertFalse(source._binary_partition(market(clobTokenIds=bad))[0])
        self.assertFalse(source._binary_partition(market(conditionId="0x" + "0"*64))[0])

    def test_malformed_member_is_not_silently_removed(self):
        for members in (None, {}, [market(), None], [market(), "bad"]):
            with self.subTest(members=members):
                with self.assertRaises(ValueError):
                    source.build_snapshot([event(members)], MODEL) if members is not None else source.build_snapshot([event(markets=None)], MODEL)

    def test_conflicting_market_ids_quarantine_both_versions(self):
        a, b = event([market(1)]), event([market(1, conditionId="0x" + "2"*64)], id="event2")
        for rows in ([a, b], [b, a]):
            result = source.build_snapshot(rows, MODEL)
            self.assertEqual(result["markets"], [])
            self.assertEqual(result["quarantined_market_ids"], ["1"])

    def test_closed_duplicate_invalidates_prior_open_copy(self):
        a = event([market(1)])
        b = event([market(1, closed=True)])
        self.assertEqual(source.build_snapshot([a, b], MODEL)["markets"], [])

    def test_token_binding_collision_quarantines_both_claims(self):
        result = source.build_snapshot([event([market(1), market(2, clobTokenIds=["2", "5"])])], MODEL)
        self.assertEqual(result["markets"], [])
        self.assertEqual(result["quarantined_market_ids"], ["1", "2"])

    def test_duplicate_negrisk_claims_are_not_complete(self):
        rows = [market(1), market(2, clobTokenIds=["2", "5"])]
        self.assertEqual(source._negrisk_attestation(event(rows), rows)[2], "DUPLICATE_MEMBER_OR_CLAIM")

    def test_mixed_market_identifiers_sort_deterministically(self):
        rows = [market(1), market(2, id="alpha"), market(3)]
        a = source.build_snapshot([event(rows)], MODEL, generated_at_ms=123)
        b = source.build_snapshot([event(list(reversed(rows)))], MODEL, generated_at_ms=123)
        self.assertEqual(a["membership_sha256"], b["membership_sha256"])
        self.assertEqual(a["markets"], b["markets"])

    def test_error_response_is_not_an_empty_success_page(self):
        for response in ({"error": "rate limit"}, {}, None, "bad", 7):
            with self.subTest(response=response):
                with self.assertRaises(ValueError):
                    source.discover_events("https://example.invalid", 1, 2, 2, lambda *_: response)

    def test_repeat_page_fails_without_exhaustive_claim(self):
        with self.assertRaisesRegex(ValueError, "pagination_drift"):
            source.discover_events("https://example.invalid", 1, 2, 3,
                                   lambda *_: [event(id="1"), event(id="2")])

    def test_overlapping_offset_pages_fail_closed(self):
        pages = iter([[event(id="1"), event(id="2")], [event(id="2")]])
        with self.assertRaisesRegex(ValueError, "pagination_drift"):
            source.discover_events("https://example.invalid", 1, 2, 3, lambda *_: next(pages))

    def test_pagination_bound_and_coverage_are_explicit(self):
        rows, status = source.discover_events("https://example.invalid", 1, 2, 1,
                                             lambda *_: [event(id="1"), event(id="2")])
        self.assertEqual(len(rows), 2)
        self.assertFalse(status["discovery_exhaustive"])
        self.assertTrue(status["pagination_loop_guard_hit"])
        self.assertFalse(status["point_in_time_consistent"])

    def test_terminal_empty_page_and_offsets(self):
        urls = []
        def fetch(url, timeout):
            urls.append(url)
            return [event(id="1")] if len(urls) == 1 else []
        rows, status = source.discover_events("https://example.invalid", 1, 1, 3, fetch)
        self.assertTrue(status["discovery_exhaustive"])
        self.assertFalse(status["point_in_time_consistent"])
        self.assertEqual([parse_qs(urlsplit(u).query)["offset"] for u in urls], [["0"], ["1"]])
        self.assertEqual(len(rows), 1)

    def test_source_error_invalidates_published_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(model_sha=MODEL, output=Path(directory)/"universe.json",
                                   status=Path(directory)/"status.json", gamma_url="https://example.invalid",
                                   timeout_seconds=1, page_size=100, max_pages=1)
            source.run_once(args, lambda *_: [event()])
            self.assertEqual(len(json.loads(args.output.read_text())["markets"]), 2)
            status = source.run_once(args, lambda *_: {"error": "outage"})
            self.assertEqual(status["state"], "SOURCE_ERROR")
            snapshot = json.loads(args.output.read_text())
            self.assertFalse(snapshot["source_valid"])
            self.assertEqual(snapshot["markets"], [])
            self.assertTrue(all(snapshot[k] is v for k, v in source.SAFETY.items()))

    def test_no_credential_bearing_source_urls(self):
        for url in ("http://example.invalid", "https://user:secret@example.invalid", "https://example.invalid?key=secret"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    source.discover_events(url, 1, 2, 2, lambda *_: [])

    def test_bad_sha_is_rejected(self):
        with self.assertRaises(ValueError):
            source.build_snapshot([], "not-a-sha")


if __name__ == "__main__":
    unittest.main()
