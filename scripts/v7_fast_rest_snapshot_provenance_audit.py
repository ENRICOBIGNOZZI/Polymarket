#!/usr/bin/env python3
"""Research-only audit for CLOB REST book snapshot provenance in V7 Fast.

Polymarket's documented POST /books response contains a per-book timestamp and hash.
This audit checks whether the repository preserves those fields through pm::Book and
whether the Fast runtime can use a fresh REST full snapshot without pretending it is
continuous WebSocket lineage. It never submits orders or mutates runtime state.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


def _slice(text: str, start: str, end: str) -> str:
    left = text.find(start)
    if left < 0:
        return ""
    right = text.find(end, left + len(start))
    return text[left:] if right < 0 else text[left:right]


def audit(root: Path) -> dict[str, Any]:
    types = (root / "include/pm/types.hpp").read_text(encoding="utf-8")
    api = (root / "src/api.cpp").read_text(encoding="utf-8")
    part2 = (root / "src/fast_runtime/part2.inc").read_text(encoding="utf-8")

    book = _slice(types, "struct Book {", "struct Market {")
    fetch_books = _slice(
        api,
        "std::unordered_map<std::string,Book> PolymarketApi::fetch_books(",
        "std::unordered_map<std::string,std::vector<PricePoint>> PolymarketApi::fetch_price_history(",
    )

    fields = {
        "book_exchange_timestamp_field": bool(
            re.search(r"\b(exchange_ts_ms|exchange_timestamp|snapshot_ts_ms|timestamp_ms)\b", book)
        ),
        "book_snapshot_hash_field": bool(re.search(r"\b(snapshot_hash|book_hash|hash)\b", book)),
        "fetch_books_parses_timestamp": 'o.find("timestamp")' in fetch_books,
        "fetch_books_parses_hash": 'o.find("hash")' in fetch_books,
        "runtime_calls_rest_un_timestamped": "Book contract carries no exchange timestamp" in part2,
        "rest_resync_clears_snapshot_ready": "ws_snapshot_ready_.erase(token);" in part2,
        "rest_resync_clears_exchange_clock": "book_exchange_ts_ms_.erase(token);" in part2,
        "rest_resync_clears_receive_clock": "book_received_ts_ms_.erase(token);" in part2,
    }

    provenance_dropped = not (
        fields["book_exchange_timestamp_field"]
        and fields["book_snapshot_hash_field"]
        and fields["fetch_books_parses_timestamp"]
        and fields["fetch_books_parses_hash"]
    )
    destructive_resync = (
        fields["runtime_calls_rest_un_timestamped"]
        and fields["rest_resync_clears_snapshot_ready"]
        and fields["rest_resync_clears_exchange_clock"]
        and fields["rest_resync_clears_receive_clock"]
    )
    blocked = provenance_dropped and destructive_resync

    return {
        "schema_version": 1,
        "research_only": True,
        "paper_only": True,
        "authenticated_execution": False,
        "documented_rest_contract": {
            "endpoint": "POST https://clob.polymarket.com/books",
            "documentation": "https://docs.polymarket.com/api-reference/market-data/get-order-books-request-body",
            "required_snapshot_fields": ["timestamp", "hash", "bids", "asks"],
        },
        "source_contract": fields,
        "blocking_provenance_loss": blocked,
        "state": "BLOCKING_REST_SNAPSHOT_PROVENANCE_DROPPED" if blocked else "REST_SNAPSHOT_PROVENANCE_PRESERVED",
        "promotion_allowed": False,
        "required_successor": [
            "Preserve each REST book's exchange timestamp and snapshot hash through the Book/API boundary.",
            "Treat a fresh REST response as a timestamped full point-in-time snapshot, not as continuous WS lineage.",
            "Evaluate synchronized REST full snapshots only when per-leg exchange age and cross-leg skew pass the same fail-closed bounds.",
            "Do not let a REST snapshot authorize later WS deltas unless continuity is re-established; reject out-of-order deltas that regress a token clock.",
            "Serialize per-token provenance before any canonical PAPER execution or promotion claim.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--output")
    args = parser.parse_args()
    report = audit(Path(args.root).resolve())
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
