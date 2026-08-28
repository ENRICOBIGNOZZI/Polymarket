import dataclasses
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from v7_function_test_support import function_test_loader, raises

import v7_semantic_mapping as s


SHA = "a" * 40


def fingerprint(venue="A", direction="YES", **changes):
    values = dict(
        venue=venue, contract_id=f"{venue}-contract", market_id=f"{venue}-market",
        event_definition="official CPI year-over-year at cutoff", entities=("US CPI",),
        event_type="NUMERIC_RELEASE", jurisdiction="US", geography="US",
        outcomes=("YES", "NO"), direction=direction, numerical_threshold="3.0",
        comparison_operator=">=", measurement_window="2026-12",
        deadline_ms=2_000_000_000_000, timezone="UTC",
        resolution_source="US Bureau of Labor Statistics",
        resolution_rules="first official release", cancellation_rules="market refunds",
        void_rules="void only if source never publishes", postponement_rules="use delayed release",
        exception_clauses="none", settlement_currency="USD",
        source_uri=f"https://{venue.lower()}.example/rules", observed_at_ms=1000,
        parser_version="v1",
    )
    values.update(changes)
    return s.ContractFingerprint(**values)


def evidence(left, right):
    return s.VerificationEvidence(
        "independent-reviewer", "AUTHORITATIVE_RULE_COMPARISON", 1200,
        (left.source_uri, right.source_uri), (left.fingerprint_hash, right.fingerprint_hash),
        s.compare_fields(left, right), SHA, 1,
    )


def test_exact_and_complement_are_field_exact_not_text_similarity():
    left, exact = fingerprint("A"), fingerprint("B")
    assert s.classify(left, exact) is s.Relationship.EXACT_EQUIVALENT
    complement = dataclasses.replace(exact, direction="NO")
    assert s.classify(left, complement) is s.Relationship.COMPLEMENT_EQUIVALENT
    changed = dataclasses.replace(exact, resolution_rules="revised release")
    assert s.classify(left, changed) is s.Relationship.NOT_EQUIVALENT


def test_verified_mapping_expires_and_detects_evidence_tampering():
    left, right = fingerprint("A"), fingerprint("B")
    mapping = s.VerifiedMapping(
        "m1", "cross_platform", left, right, s.Relationship.EXACT_EQUIVALENT,
        s.MappingState.VERIFIED, evidence(left, right), 1000, 2000,
    )
    mapping.validate(now_ms=1500, repository_sha=SHA)
    with raises(s.SemanticMappingError, "expired"):
        mapping.validate(now_ms=2000, repository_sha=SHA)
    broken = dataclasses.replace(mapping, right=dataclasses.replace(right, timezone="America/New_York"))
    with raises(s.SemanticMappingError, "relationship"):
        broken.validate(now_ms=1500, repository_sha=SHA)


def test_empty_registry_is_valid_but_never_manufactures_a_mapping():
    rows = s.load_verified_mappings(
        ROOT / "config" / "v7_external_mappings.json", "sports_latency",
        now_ms=1000, repository_sha=SHA,
    )
    assert rows == ()
    assert 0.0 <= s.candidate_score(fingerprint("A"), fingerprint("B")) <= 1.0


load_tests = function_test_loader(globals())
