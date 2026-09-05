from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    source = (ROOT / "src/v7_crypto_book_observer.cpp").read_text()
    header = (ROOT / "include/pm/v7_crypto_book_observer.hpp").read_text()
    tape = (ROOT / "include/pm/v7_crypto_book_tape.hpp").read_text()
    entry = (ROOT / "src/v7_crypto_book_observer_main.cpp").read_text()
    cmake = (ROOT / "CMakeLists.txt").read_text()
    loop = (ROOT / "scripts/paper_v7_execution_loop.sh").read_text()

    assert "RESEARCH_EVIDENCE_ONLY" in source
    assert '"paper_only", true' in source
    assert '"authenticated_execution", false' in source
    assert '"real_order_submission", false' in source
    assert "collector_code_sha" in source
    assert "observed_runtime_sha" in source
    assert "--collector-sha" in entry
    assert "--runtime-sha" in entry
    assert "--evidence-root" in entry
    assert "research evidence root must differ" in entry
    assert "research evidence root must be outside observed live run root" in source
    assert 'evidence_dir(evidence_root / "normalized_events")' in source
    assert "MarketWsEventKind::BookChanged" in source
    assert "ExternalTapeRecorder" in source
    assert "TapeSegmentOptions{kSegmentBytes, kSegmentSeconds}" in source
    assert "64ULL * 1024ULL * 1024ULL" in source
    assert "kSegmentSeconds = 300" in source
    assert "external_fair/status.json" in source
    assert "CryptoBookTapePayload" in source
    assert "kCryptoBookTapeSchemaVersion" in source
    assert "CryptoBookTapePayload" in tape
    assert "BookHotSnapshot book" in tape
    assert "class CryptoBookObserver" in header
    assert "polymarket_v7_crypto_book_observer" in cmake
    assert "CryptoBookObserver observer" in entry
    # Research binary exists for explicit evidence collection only. The
    # canonical process manifest/loop must not gain another runtime owner.
    assert "polymarket_v7_crypto_book_observer" not in loop
    assert "PM_V7_REAL_ORDER_SUBMISSION" not in entry


if __name__ == "__main__":
    main()
