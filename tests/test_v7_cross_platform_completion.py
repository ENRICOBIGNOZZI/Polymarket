import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v7_cross_platform as c


FIELDS = (
    "normalized_event", "resolution_source", "cutoff_ms", "timezone", "comparator",
    "rounding", "cancellation_rules", "exception_rules", "payout",
    "rules_hash", "outcome_semantics", "settlement_currency",
)


def source(venue="A", verified=True):
    return c.VenueSourceSpec(
        venue, f"wss://{venue.lower()}.example/books", "v1", "schema",
        "STRICT_MONOTONIC_PER_CONTRACT", "RESUME_AFTER_SEQUENCE", 1000, verified,
    )


def book_event(seq, *, kind=c.BookUpdateType.SNAPSHOT, venue="A", event_id=None, receive=1010,
               bids=None, asks=None):
    return c.VenueBookEvent(
        venue, "cid", seq, kind,
        tuple(bids if bids is not None else [c.DepthLevel(.40, 20)]),
        tuple(asks if asks is not None else [c.DepthLevel(.45, 20)]),
        receive - 5, receive, "v1", event_id or f"{venue}-{seq}",
    )


def contract(venue, outcome):
    return c.CrossVenueContract(
        venue, f"{venue}-contract", "canonical-event", outcome, "Official Agency",
        10_000, "UTC", ">=", "none", "refund", "none", 1.0,
        "canonical-rules", "binary_at_cutoff", "USD",
    )


def verification():
    return c.SemanticVerification(
        "reviewer", 1000, "https://official.example/rules", "evidence", FIELDS,
    )


def equivalent(kind=c.EquivalenceType.COMPLEMENT):
    a = contract("A", "YES")
    b = contract("B", "NO" if kind is c.EquivalenceType.COMPLEMENT else "YES")
    return c.ContractEquivalence(a, b, kind, c.equivalence_hash(a, b, kind), True, verification())


def fee(venue, cid, sequence, notional=4.5):
    return c.AuthoritativeFeeQuote(
        venue, cid, "schedule-v1", "https://official.example/fees", "fee-hash",
        10, notional, .05, 2000, 1000, sequence, True,
    )


def balance(venue, available=10):
    return c.PaperBalanceSnapshot(venue, "USD", available, 0, 2000, f"{venue}-balance")


def joint_probabilities():
    return {"NONE": 0.0, "A_ONLY": 0.0, "B_ONLY": 0.0, "FULL": 1.0}


def joint_pnl():
    return {"NONE": 0.0, "A_ONLY": -1.0, "B_ONLY": -1.0, "FULL": 0.0}


def test_verified_read_only_adapter_and_causal_book_resume():
    def decoder(payload, receive_ts_ms, spec):
        row = json.loads(payload)
        return book_event(row["seq"], venue=spec.venue, receive=receive_ts_ms)

    adapter = c.VerifiedVenueAdapter(source(), decoder)
    assert adapter.decode_book(b'{"seq":1}', 1010).sequence == 1
    with pytest.raises(c.CrossVenueError, match="unverified_venue_source"):
        c.VerifiedVenueAdapter(source(verified=False), decoder)

    tape = c.VenueBookTape("A", "cid", max_age_ms=100)
    first = book_event(1)
    tape.apply(first, now_ms=1010)
    tape.apply(first, now_ms=1011)
    assert tape.usable(1011) and tape.records[-1].reason == "idempotent_duplicate"
    tape.disconnect(); tape.begin_resume(resume_after_sequence=1)
    tape.apply(book_event(
        2, kind=c.BookUpdateType.DELTA, receive=1020,
        asks=[c.DepthLevel(.45, 0), c.DepthLevel(.44, 10)], bids=[],
    ), now_ms=1020)
    assert tape.sequence == 2 and tape.ask_levels()[0].price == .44


def test_book_gap_cross_and_staleness_quarantine_only_that_venue():
    tape = c.VenueBookTape("A", "cid", max_age_ms=10)
    tape.apply(book_event(1), now_ms=1010)
    tape.apply(book_event(3, kind=c.BookUpdateType.DELTA, receive=1011), now_ms=1011)
    assert tape.state is c.BookConnectionState.QUARANTINED and tape.blocker == "book_gap"

    crossed = c.VenueBookTape("A", "cid", max_age_ms=10)
    crossed.apply(book_event(1, bids=[c.DepthLevel(.5, 1)], asks=[c.DepthLevel(.5, 1)]), now_ms=1010)
    assert crossed.blocker == "crossed_or_locked_book"

    stale = c.VenueBookTape("A", "cid", max_age_ms=10)
    stale.apply(book_event(1), now_ms=1010)
    assert not stale.usable(1021) and stale.blocker == "stale_book"


def test_semantics_require_all_fields_and_authoritative_attestation():
    eq = equivalent()
    assert eq.hard_arb_authorized
    missing = c.ContractEquivalence(
        eq.contract_a, eq.contract_b, eq.equivalence_type, eq.semantic_hash, True, None,
    )
    assert not missing.hard_arb_authorized

    changed = c.CrossVenueContract(**{
        **asdict_contract(eq.contract_b), "cutoff_ms": 10_001,
    })
    assert c.classify(eq.contract_a, changed) is c.EquivalenceType.NOT_EQUIVALENT


def asdict_contract(contract):
    return {name: getattr(contract, name) for name in contract.__dataclass_fields__}


def test_exact_plan_uses_fee_quotes_full_depth_and_paper_balances():
    eq = equivalent()
    plan = c.plan_cross_venue_exact(
        eq, quantity=10, asks_a=[c.DepthLevel(.45, 10)], asks_b=[c.DepthLevel(.45, 10)],
        fee_quote_a=fee("A", "A-contract", 1), fee_quote_b=fee("B", "B-contract", 2),
        balance_a=balance("A"), balance_b=balance("B"),
        execution_state_probabilities=joint_probabilities(), state_pnl_adjustments=joint_pnl(),
        transfer_cost=0, duration_seconds=10,
    )
    assert plan.executable and plan.expected_net_pnl == pytest.approx(.9)

    blocked = c.plan_cross_venue_exact(
        eq, quantity=10, asks_a=[c.DepthLevel(.45, 10)], asks_b=[c.DepthLevel(.45, 10)],
        fee_quote_a=fee("A", "A-contract", 1), fee_quote_b=fee("B", "B-contract", 2),
        balance_a=balance("A", 1), balance_b=balance("B"),
        execution_state_probabilities=joint_probabilities(), state_pnl_adjustments=joint_pnl(),
        transfer_cost=0, duration_seconds=10,
    )
    assert not blocked.executable and blocked.blocker == "prepositioned_balance_insufficient"

    with pytest.raises(c.CrossVenueError, match="fee_quote_notional_mismatch"):
        c.plan_cross_venue_exact(
            eq, quantity=10, asks_a=[c.DepthLevel(.45, 10)], asks_b=[c.DepthLevel(.45, 10)],
            fee_quote_a=fee("A", "A-contract", 1, 4.4), fee_quote_b=fee("B", "B-contract", 2),
            balance_a=balance("A"), balance_b=balance("B"),
            execution_state_probabilities=joint_probabilities(), state_pnl_adjustments=joint_pnl(),
            transfer_cost=0, duration_seconds=10,
        )


def test_same_outcome_two_buys_are_not_mislabeled_guaranteed_arb():
    eq = equivalent(c.EquivalenceType.EXACT_EQUIVALENT)
    assert eq.hard_arb_authorized
    plan = c.plan_cross_venue(
        eq, quantity=1, asks_a=[c.DepthLevel(.4, 1)], asks_b=[c.DepthLevel(.4, 1)],
        fee_a=0, fee_b=0, slippage_bps=0, transfer_cost=0,
        execution_state_probabilities=joint_probabilities(), state_pnl_adjustments=joint_pnl(),
        balances={"A": 1, "B": 1}, duration_seconds=1,
    )
    assert not plan.executable and plan.blocker == "non_complement_legs_not_guaranteed"


def race(opportunity, bundle, a, b):
    return c.JointRaceObservation(
        opportunity, bundle, 1000, 1100, 10, a, b,
        1050 if a else None, 1060 if b else None, 1, 2,
    )


def test_joint_race_tape_preserves_dependence_and_independent_bundle_units():
    tape = c.JointRaceTape()
    tape.append(race("o1", "bundle-1", 10, 0))
    tape.append(race("o2", "bundle-2", 10, 10))
    probabilities = tape.probabilities(minimum_independent_bundles=2)
    assert probabilities == {"NONE": 0, "A_ONLY": .5, "B_ONLY": 0, "FULL": .5}
    with pytest.raises(c.CrossVenueError, match="duplicate_joint_race"):
        tape.append(race("o1", "bundle-3", 0, 0))


def test_cross_platform_forward_shadow_has_exact_lineage_and_no_real_orders(tmp_path):
    eq = equivalent()
    quote_a, quote_b = fee("A", "A-contract", 1), fee("B", "B-contract", 2)
    bal_a, bal_b = balance("A"), balance("B")
    plan = c.plan_cross_venue_exact(
        eq, quantity=10, asks_a=[c.DepthLevel(.45, 10)], asks_b=[c.DepthLevel(.45, 10)],
        fee_quote_a=quote_a, fee_quote_b=quote_b, balance_a=bal_a, balance_b=bal_b,
        execution_state_probabilities=joint_probabilities(), state_pnl_adjustments=joint_pnl(),
        transfer_cost=0, duration_seconds=10,
    )
    observation = race("o1", "bundle", 10, 10)
    output = tmp_path / "cross-shadow.jsonl"
    record_hash = c.CrossPlatformForwardShadow(output, run_id="run").record(
        equivalence=eq, plan=plan, observed_at_ms=1200, book_sequence_a=1,
        book_sequence_b=2, fee_quote_a=quote_a, fee_quote_b=quote_b,
        balance_a=bal_a, balance_b=bal_b, race=observation,
    )
    row = json.loads(output.read_text())
    assert row["record_hash"] == record_hash and row["race_state"] == "FULL"
    assert row["paper_only"] and not row["real_order_submission"]
