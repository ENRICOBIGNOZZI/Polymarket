#!/usr/bin/env python3
from __future__ import annotations

import sys
import copy
import json
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v7_external_fair_research as router  # noqa: E402
from v7_external_fair_research import (  # noqa: E402
    Book, entry_tte_allowed, executable_sell_value, fee_per_share,
    hybrid_probability, live_market_yes, model_market_disagreement_allowed,
    opportunity_set, probability_interval_bin_diagnostics, robust_candidates,
)
from v7_opportunity import OpportunityEnvelope  # noqa: E402
from v7_ledger_spool import drain_spool  # noqa: E402


def snapshot() -> dict:
    current = time.monotonic_ns()
    return {
        "code_sha": "a" * 40, "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False,
        "contract": {"verified": True, "rules_hash_recognized": True, "rules_hash": "d" * 64},
        "settlement_reference": {"valid": True},
        "oracle": {"healthy": True, "continuity": "LIVE_CONTINUOUS"},
        "external": {"healthy": True},
        "fair": {"valid": True, "yes": 0.77, "pm_mid": 0.72,
                 "gamma_discovery_mid_diagnostic": 0.485,
                 "lower": 0.75, "upper": 0.80, "tte_seconds": 45.0,
                 "calculated_monotonic_ns": current - 1, "valid_until_monotonic_ns": current + 1_000_000_000},
        "market": {"yes_token": "yes", "no_token": "no",
                   "fee_schedule": {"rate": 0.07, "exponent": 1, "takerOnly": True}},
    }


def book(token: str, ask: float, bid: float | None = None) -> Book:
    return Book(token, ((ask - 0.01 if bid is None else bid, 100.0),), ((ask, 100.0),), 0.01, 5.0,
                1_000, 1_001, f"book-{token}")







def test_empty_candidate_input_reason_is_not_false_no_edge() -> None:
    base = snapshot()
    now = time.monotonic_ns()
    base["fair"].update(calculated_monotonic_ns=now - 1, valid_until_monotonic_ns=now + 10_000_000_000)
    assert router.candidate_input_rejection_reason(base, current_ns=now) == ""
    cases = [
        ("contract", "verified", False, "CONTRACT_RULES_NOT_VERIFIED"),
        ("settlement_reference", "valid", False, "SETTLEMENT_REFERENCE_NOT_CAPTURED"),
        ("oracle", "healthy", False, "ORACLE_NOT_READY"),
        ("external", "healthy", False, "EXTERNAL_FEEDS_NOT_READY"),
        ("fair", "valid", False, "FAIR_VALUE_INVALID"),
        ("fair", "calculated_monotonic_ns", now + 1, "FAIR_SNAPSHOT_CLOCK_INVALID"),
        ("fair", "calculated_monotonic_ns", "bad", "FAIR_SNAPSHOT_CLOCK_INVALID"),
        ("fair", "valid_until_monotonic_ns", now - 1, "FAIR_SNAPSHOT_EXPIRED"),
    ]
    for section, key, value, expected in cases:
        observation = copy.deepcopy(base)
        observation[section][key] = value
        if expected == "FAIR_SNAPSHOT_EXPIRED":
            observation["fair"]["calculated_monotonic_ns"] = now - 10
        assert router.candidate_input_rejection_reason(observation, current_ns=now) == expected






def test_incremental_counterfactual_index_parity_and_invalidation() -> None:
    def row(identity, kind="OPPORTUNITY_SET", sha="a" * 40, value=1):
        return {"record_id": identity, "event_type": kind, "model_sha": sha,
                "timestamp_ms": 1, "value": value}
    def write(path, rows, mode="wb"):
        with path.open(mode) as handle:
            for value in rows:
                handle.write((json.dumps(value, sort_keys=True) + "\n").encode())
    def reference(paths):
        result = {}
        for path in paths:
            if not path.exists(): continue
            with path.open("rb") as handle:
                for line in handle:
                    if not line.endswith(b"\n") or not line.strip(): continue
                    value = json.loads(line); key = value["record_id"]
                    if key in result: assert result[key] == value
                    result.setdefault(key, value)
        return result
    with tempfile.TemporaryDirectory() as directory:
        a, b = [Path(directory) / name for name in ("durable", "active")]
        write(a, [row(str(i)) for i in range(1000)])
        write(b, [row("fill", "VIRTUAL_FILL"), row("other", sha="b" * 40)])
        paths = [a, b]; index = router._CounterfactualIndex(paths)
        try:
            assert list(index.iter_records()) == list(reference(paths).items())
            assert len(dict(index.iter_records())) == 1002
            assert index.metrics["last_bytes_read"] == 0
            assert index.metrics["last_records_decoded"] == 0
            write(a, [row("fill", "VIRTUAL_FILL")], "ab")
            assert list(index.iter_records()) == list(reference(paths).items())
            assert index.metrics["last_records_decoded"] == 1
            selected = dict(index.iter_records(event_types=("VIRTUAL_FILL",), model_sha="a"*40))
            assert selected == {"fill": row("fill", "VIRTUAL_FILL")}
            partial = json.dumps(row("partial")).encode()
            with b.open("ab") as handle: handle.write(partial[:20])
            assert "partial" not in dict(index.iter_records())
            with b.open("ab") as handle: handle.write(partial[20:] + b"\n")
            assert list(index.iter_records()) == list(reference(paths).items())
            write(b, [row("1", value=2)], "ab")
            for _ in range(2):
                try: dict(index.iter_records(event_types=("VIRTUAL_FILL",)))
                except RuntimeError as exc: assert "conflict" in str(exc)
                else: raise AssertionError("conflicting source accepted")
            write(b, [row("fixed")])
            assert list(index.iter_records()) == list(reference(paths).items())
            replacement = Path(directory) / "replacement"
            write(replacement, [row("replacement")]); replacement.replace(a)
            assert list(index.iter_records()) == list(reference(paths).items())
            write(a, [row("replacement", value=2)])
            assert dict(index.iter_records())["replacement"]["value"] == 2
            a.unlink()
            assert list(index.iter_records()) == list(reference(paths).items())
            with b.open("ab") as handle: handle.write(b"{malformed}\n")
            try: dict(index.iter_records())
            except RuntimeError as exc: assert "invalid" in str(exc)
            else: raise AssertionError("malformed complete record accepted")
            write(b, [row("fixed")]); assert list(index.iter_records()) == list(reference(paths).items())
            a.symlink_to(b)
            try: dict(index.iter_records())
            except RuntimeError as exc: assert "symlink" in str(exc)
            else: raise AssertionError("symlink accepted")
            a.unlink()
        finally:
            index.close()
        rebuilt = router._CounterfactualIndex(paths)
        try: assert list(rebuilt.iter_records()) == list(reference(paths).items())
        finally: rebuilt.close()




def test_forward_test_events_are_excluded_from_legacy_fair_accounting() -> None:
    base = {
        "component": router.COMPONENT, "paper_exploration": True,
        "economic_authority": "PAPER_EXPLORATION", "counterfactual": False,
        "excluded_from_portfolio_equity": False, "research_evidence_only": False,
    }
    legacy = router.LedgerEvent(
        event_type="FILL", strategy=router.STRATEGY, model_sha="a" * 40, metadata=base,
    )
    forward = router.LedgerEvent(
        event_type="FILL", strategy=router.STRATEGY, model_sha="a" * 40,
        metadata={**base, "paper_forward_test": True},
    )
    assert router._canonical_paper_exploration_event(legacy) is True
    assert router._canonical_paper_exploration_event(forward) is False




