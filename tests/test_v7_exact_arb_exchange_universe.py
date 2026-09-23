from __future__ import annotations

from pathlib import Path
import sys
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from v7_exact_arb_exchange_universe import SAFETY, build_snapshot, discover_events
from v7_unified_exact_arb_graph import compile_graph


MODEL = "a" * 40


def _market(mid: str, *, neg: bool = False, active: bool = True, accepting: bool = True) -> dict:
    condition = "0x" + (mid[-1] if mid[-1] in "0123456789abcdef" else "a") * 64
    base = int(mid) if mid.isdigit() else 1
    return {
        "id": mid,
        "conditionId": condition,
        "question": f"Question {mid}",
        "slug": f"m-{mid}",
        "outcomes": '["Yes","No"]',
        "clobTokenIds": f'["{1000+base}","{2000+base}"]',
        "active": active,
        "closed": False,
        "acceptingOrders": accepting,
        "enableOrderBook": True,
        "negRisk": neg,
        "orderPriceMinTickSize": 0.01,
        "orderMinSize": 5,
        "feesEnabled": False,
        "feeSchedule": {"rate": 0, "exponent": 1},
        "startDate": "2026-09-23T00:00:00Z",
        "endDate": "2026-09-24T00:00:00Z",
    }


def _event(eid: str, markets: list[dict], *, neg: bool = False, augmented: bool = False) -> dict:
    return {
        "id": eid,
        "slug": f"e-{eid}",
        "active": True,
        "closed": False,
        "negRisk": neg,
        "enableNegRisk": neg,
        "negRiskAugmented": augmented,
        "markets": markets,
    }


def _registry() -> dict:
    return {
        **SAFETY,
        "schema": "polymarket_v7_exact_arb_relation_registry_v1",
        "version": 1,
        "relations": [],
    }


def test_public_binary_ctf_market_auto_compiles_without_cross_market_semantic_claim() -> None:
    snapshot = build_snapshot([_event("10", [_market("1")])], MODEL, generated_at_ms=1)
    assert snapshot["schema"] == "polymarket_v7_exact_arb_exchange_universe_v1"
    row = snapshot["markets"][0]
    assert row["binary_partition_verified"] is True
    assert len(row["settlement_semantic_hash"]) == 64
    assert row["normalized_rules_hash"] == ""
    assert row["neg_risk_complete_set_verified"] is False
    graph = compile_graph([_registry()], snapshot, MODEL)
    assert len(graph["relations"]) == 1
    assert graph["relations"][0]["relation_family"] == "SAME_MARKET_BINARY_COMPLETE_SET"


def test_non_augmented_negrisk_event_remains_unverified_without_independent_membership_proof() -> None:
    members = [_market(str(i), neg=True) for i in (1, 2, 3)]
    snapshot = build_snapshot([_event("20", members, neg=True)], MODEL, generated_at_ms=1)
    assert snapshot["verified_negrisk_events"] == 0
    assert snapshot["negrisk_rejection_reasons"]["INDEPENDENT_COMPLETE_SET_ATTESTATION_REQUIRED"] == 1
    candidate = snapshot["unverified_candidates"][0]
    assert candidate["relation_family"] == "NEGRISK_COMPLETE_SET"
    assert candidate["verification"] == "UNVERIFIED_CANDIDATE"
    assert all(row["neg_risk_complete_set_verified"] is False for row in snapshot["markets"])
    graph = compile_graph([_registry()], snapshot, MODEL)
    families = [row["relation_family"] for row in graph["relations"]]
    assert families.count("SAME_MARKET_BINARY_COMPLETE_SET") == 3
    assert "NEGRISK_COMPLETE_SET" not in families


def test_augmented_negrisk_is_never_auto_attested() -> None:
    members = [_market(str(i), neg=True) for i in (1, 2, 3)]
    snapshot = build_snapshot([_event("30", members, neg=True, augmented=True)], MODEL, generated_at_ms=1)
    assert snapshot["verified_negrisk_events"] == 0
    assert snapshot["augmented_negrisk_events"] == 1
    assert snapshot["negrisk_rejection_reasons"]["AUGMENTED_NEGRISK_UNSTABLE_MEMBERSHIP"] == 1
    assert all(row["neg_risk_complete_set_verified"] is False for row in snapshot["markets"])
    graph = compile_graph([_registry()], snapshot, MODEL)
    assert all(row["relation_family"] != "NEGRISK_COMPLETE_SET" for row in graph["relations"])


def test_negrisk_requires_every_member_simultaneously_orderable() -> None:
    members = [_market("1", neg=True), _market("2", neg=True, accepting=False)]
    snapshot = build_snapshot([_event("40", members, neg=True)], MODEL, generated_at_ms=1)
    assert snapshot["verified_negrisk_events"] == 0
    assert snapshot["negrisk_rejection_reasons"]["MEMBER_NOT_SIMULTANEOUSLY_ORDERABLE"] == 1
    assert len(snapshot["markets"]) == 1
    assert snapshot["markets"][0]["neg_risk_complete_set_verified"] is False


def test_invalid_ctf_identity_remains_unverified() -> None:
    market = _market("1")
    market["conditionId"] = "not-a-condition"
    snapshot = build_snapshot([_event("50", [market])], MODEL, generated_at_ms=1)
    assert snapshot["markets"][0]["binary_partition_verified"] is False
    assert snapshot["markets"][0]["settlement_semantic_hash"] == ""
    graph = compile_graph([_registry()], snapshot, MODEL)
    assert graph["relations"] == []


def test_event_pagination_is_bounded_and_exhaustive_when_final_page_short() -> None:
    calls = []

    def fetcher(url: str, timeout: float):
        calls.append((url, timeout))
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        offset = int(query["offset"][0])
        if offset == 0:
            return [_event("1", [_market("1")]), _event("2", [_market("2")])]
        if offset == 2:
            return [_event("3", [_market("3")])]
        raise AssertionError(offset)

    events, diagnostics = discover_events(
        "https://gamma.example", 1.0, page_size=2, max_pages=5, fetcher=fetcher
    )
    assert {event["id"] for event in events} == {"1", "2", "3"}
    assert diagnostics["discovery_exhaustive"] is True
    assert diagnostics["pagination_loop_guard_hit"] is False
    assert diagnostics["requests"] == 2
    assert all("active=true" in url and "closed=false" in url for url, _ in calls)


def test_negrisk_market_membership_hash_changes_when_event_membership_changes() -> None:
    left = build_snapshot([_event("60", [_market("1", neg=True), _market("2", neg=True)], neg=True)], MODEL, generated_at_ms=1)
    right = build_snapshot([_event("60", [_market("1", neg=True), _market("2", neg=True), _market("3", neg=True)], neg=True)], MODEL, generated_at_ms=1)
    assert left["membership_sha256"] != right["membership_sha256"]
