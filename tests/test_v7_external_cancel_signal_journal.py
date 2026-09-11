from __future__ import annotations

import sys
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v7_external_cancel_signal_journal as journal  # noqa: E402


def signal(*, valid: bool = True, version: int = 7) -> dict:
    return {
        "schema": journal.SIGNAL_SCHEMA,
        "code_sha": "a" * 40,
        "rule_sha256": "b" * 64,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "execution_authority": "ZERO_AUTHORITY_SIGNAL_ONLY",
        "supported_cancel_side": "BUY",
        "stale_buy_outcome": "NO",
        "signal_version": version,
        "started_monotonic_ns": 100,
        "trigger_receive_wall_ns": 1_000,
        "publish_wall_ns": 1_100,
        "valid": valid,
    }


def test_signal_contract_and_transition_identity() -> None:
    row = signal(valid=True)
    assert journal.validate_signal(row, "a" * 40) == row
    expired = signal(valid=False)
    assert journal.signal_key(row) != journal.signal_key(expired)
    assert journal.signal_key(row) == journal.signal_key(dict(row))


def test_journaler_deduplicates_polling_and_preserves_validity_transition() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        signal_path = root / "signal.json"
        output = root / "events.jsonl"
        status = root / "status.json"
        signal_path.write_text(json.dumps(signal(valid=True)), encoding="utf-8")
        args = SimpleNamespace(
            signal=signal_path, output=output, status=status,
            model_sha="a" * 40, interval_ms=10, maximum_hot_bytes=1 << 20,
        )
        worker = journal.Journaler(args)
        try:
            worker.tick(); worker.tick()
            first = list(journal.journal_rows(output))
            assert len(first) == 1
            assert first[0]["asset"] == "BTC" and first[0]["horizon"] == "M5"
            assert first[0]["journal_execution_authority"] == "ZERO_AUTHORITY_RESEARCH_ONLY"
            signal_path.write_text(json.dumps(signal(valid=False)), encoding="utf-8")
            worker.tick(); worker.tick(); worker.publish()
            rows = list(journal.journal_rows(output))
            assert len(rows) == 2
            assert [row["valid"] for row in rows] == [True, False]
            state = json.loads(status.read_text(encoding="utf-8"))
            assert state["appended_transitions"] == 2
            assert state["execution_authority"] == "ZERO_AUTHORITY_RESEARCH_ONLY"
        finally:
            worker.journal.close()


def test_wrong_authority_and_timing_fail_closed() -> None:
    bad = signal()
    bad["execution_authority"] = "OMS"
    assert journal.validate_signal(bad, "a" * 40) is None
    bad = signal()
    bad["publish_wall_ns"] = 999
    assert journal.validate_signal(bad, "a" * 40) is None
    bad = signal()
    bad["rule_sha256"] = "short"
    assert journal.validate_signal(bad, "a" * 40) is None
    bad = signal()
    bad["valid"] = None
    assert journal.validate_signal(bad, "a" * 40) is None


if __name__ == "__main__":
    test_signal_contract_and_transition_identity()
    test_journaler_deduplicates_polling_and_preserves_validity_transition()
    test_wrong_authority_and_timing_fail_closed()
