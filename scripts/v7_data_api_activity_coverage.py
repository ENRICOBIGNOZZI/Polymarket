#!/usr/bin/env python3
"""Prove complete, time-bounded Data API activity coverage from sealed pages.

The collector must request documented ascending timestamp windows and capture
every offset page through a short terminal page. This is a decoder only: it
does not issue HTTP requests or write an evidence tape.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Iterable
from urllib.parse import urlparse


ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
QUERY_KEYS = {"user", "offset", "limit", "start", "end", "sortBy", "sortDirection"}
MAX_LIMIT = 500
MAX_OFFSET = 10_000


class ActivityCoverageError(ValueError):
    pass


@dataclass(frozen=True)
class ActivityCoverage:
    model_sha: str
    wallet: str
    capture_start_ts: int
    capture_end_ts: int
    pages: tuple[tuple[int, int, int, str], ...]
    activity_count: int

    def to_dict(self) -> dict[str, Any]:
        return {"schema": "polymarket_v7_data_api_activity_coverage_v1", "model_sha": self.model_sha,
                "wallet": self.wallet, "capture_start_ts": self.capture_start_ts,
                "capture_end_ts": self.capture_end_ts,
                "pages": [{"start": start, "end": end, "offset": offset, "evidence_record_hash": record_hash}
                          for start, end, offset, record_hash in self.pages],
                "activity_count": self.activity_count}


def _wallet(value: Any) -> str:
    if not isinstance(value, str) or not ADDRESS_RE.fullmatch(value):
        raise ActivityCoverageError("wallet:invalid")
    return value.lower()


def _integer(value: Any, code: str) -> int:
    if not isinstance(value, str) or not value.isdigit():
        raise ActivityCoverageError(code)
    return int(value)


def _row_identity(row: dict[str, Any]) -> str:
    """A full duplicate is never another activity; redeems require outcome identity."""
    if row.get("type") == "REDEEM":
        tx, asset, outcome = row.get("transactionHash"), row.get("asset"), row.get("outcomeIndex")
        if (not isinstance(tx, str) or not tx or not isinstance(asset, str) or not asset
                or isinstance(outcome, bool) or not isinstance(outcome, int) or outcome < 0):
            raise ActivityCoverageError("activity:redeem_outcome_identity")
        return f"REDEEM:{tx.lower()}:{asset}:{outcome}"
    try:
        payload = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                             allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ActivityCoverageError("activity:row_not_canonical") from exc
    return "ROW:" + hashlib.sha256(payload).hexdigest()


def activity_coverage(records: Iterable[Any], *, wallet: str) -> ActivityCoverage:
    """Return a strictly contiguous timestamp-window coverage manifest."""
    wallet = _wallet(wallet)
    model_sha: str | None = None
    windows: dict[tuple[int, int], dict[int, tuple[int, list[dict[str, Any]], str]]] = {}
    for record in tuple(records):
        if (getattr(record, "source", None) != "DATA_API_ACTIVITY"
                or not isinstance(getattr(record, "model_sha", None), str)
                or not SHA_RE.fullmatch(record.model_sha)
                or not isinstance(getattr(record, "record_hash", None), str)
                or not SHA256_RE.fullmatch(record.record_hash)):
            raise ActivityCoverageError("evidence:must_be_sealed_data_api_activity")
        if model_sha is None:
            model_sha = record.model_sha
        elif model_sha != record.model_sha:
            raise ActivityCoverageError("evidence:mixed_model_sha")
        endpoint, query, response = urlparse(str(getattr(record, "endpoint", ""))), getattr(record, "query", None), getattr(record, "response", None)
        if (endpoint.scheme != "https" or endpoint.netloc != "data-api.polymarket.com" or endpoint.path != "/activity"
                or not isinstance(query, dict) or set(query) != QUERY_KEYS or not isinstance(response, list)):
            raise ActivityCoverageError("evidence:activity_request_shape")
        if (str(query.get("user") or "").lower() != wallet or query.get("sortBy") != "TIMESTAMP"
                or query.get("sortDirection") != "ASC"):
            raise ActivityCoverageError("evidence:activity_request_scope")
        offset = _integer(query.get("offset"), "evidence:activity_pagination")
        limit = _integer(query.get("limit"), "evidence:activity_pagination")
        start = _integer(query.get("start"), "evidence:activity_window")
        end = _integer(query.get("end"), "evidence:activity_window")
        if offset > MAX_OFFSET or not 0 < limit <= MAX_LIMIT or end < start or len(response) > limit:
            raise ActivityCoverageError("evidence:activity_pagination")
        if any(not isinstance(item, dict) for item in response):
            raise ActivityCoverageError("evidence:activity_row")
        pages = windows.setdefault((start, end), {})
        if offset in pages:
            raise ActivityCoverageError("evidence:activity_duplicate_offset")
        pages[offset] = (limit, response, record.record_hash)
    if model_sha is None or not windows:
        raise ActivityCoverageError("coverage:first_page_missing")
    output_pages: list[tuple[int, int, int, str]] = []
    seen_rows: set[str] = set()
    count = 0
    previous_end: int | None = None
    for (start, end), pages in sorted(windows.items()):
        if previous_end is not None and start != previous_end + 1:
            raise ActivityCoverageError("coverage:time_window_gap")
        previous_end = end
        expected, terminal = 0, False
        for offset, (limit, response, record_hash) in sorted(pages.items()):
            if offset != expected:
                raise ActivityCoverageError("coverage:offset_gap")
            if terminal:
                raise ActivityCoverageError("coverage:page_after_terminal")
            for row in response:
                timestamp = row.get("timestamp")
                if (isinstance(timestamp, bool) or not isinstance(timestamp, (int, str))
                        or not str(timestamp).isdigit() or not start <= int(timestamp) <= end):
                    raise ActivityCoverageError("activity:timestamp_window")
                identity = _row_identity(row)
                if identity in seen_rows:
                    raise ActivityCoverageError("activity:duplicate_row")
                seen_rows.add(identity)
            output_pages.append((start, end, offset, record_hash))
            count += len(response)
            expected += limit
            terminal = len(response) < limit
        if not terminal:
            raise ActivityCoverageError("coverage:terminal_short_page_missing")
    first_start, last_end = min(window[0] for window in windows), max(window[1] for window in windows)
    return ActivityCoverage(model_sha, wallet, first_start, last_end, tuple(output_pages), count)
