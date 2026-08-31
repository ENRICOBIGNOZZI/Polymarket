#!/usr/bin/env python3
"""Prove a complete, paginated Data API activity observation window.

The input consists solely of sealed read-only Data API pages. This module
checks the page offsets, explicit inclusion of deposits/withdrawals, and a
terminal short page. It is a decoder; it performs no HTTP request or write.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable
from urllib.parse import urlparse


ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
QUERY_KEYS = {"user", "offset", "limit", "excludeDepositsWithdrawals"}


class ActivityCoverageError(ValueError):
    pass


@dataclass(frozen=True)
class ActivityCoverage:
    model_sha: str
    wallet: str
    pages: tuple[tuple[int, str], ...]
    activity_count: int

    def to_dict(self) -> dict[str, Any]:
        return {"schema": "polymarket_v7_data_api_activity_coverage_v1", "model_sha": self.model_sha,
                "wallet": self.wallet, "pages": [{"offset": offset, "evidence_record_hash": record_hash}
                                                     for offset, record_hash in self.pages],
                "activity_count": self.activity_count}


def _wallet(value: Any) -> str:
    if not isinstance(value, str) or not ADDRESS_RE.fullmatch(value):
        raise ActivityCoverageError("wallet:invalid")
    return value.lower()


def activity_coverage(records: Iterable[Any], *, wallet: str) -> ActivityCoverage:
    """Return coverage only for contiguous pages ending at a short page."""
    records = tuple(records)
    wallet = _wallet(wallet)
    pages: dict[int, tuple[int, str]] = {}
    model_sha: str | None = None
    for record in records:
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
        if query["user"].lower() != wallet or query["excludeDepositsWithdrawals"].lower() != "false":
            raise ActivityCoverageError("evidence:activity_request_scope")
        if not query["offset"].isdigit() or not query["limit"].isdigit() or int(query["limit"]) <= 0:
            raise ActivityCoverageError("evidence:activity_pagination")
        offset, limit = int(query["offset"]), int(query["limit"])
        if len(response) > limit or offset in pages:
            raise ActivityCoverageError("evidence:activity_pagination")
        if any(not isinstance(item, dict) for item in response):
            raise ActivityCoverageError("evidence:activity_row")
        pages[offset] = (limit, record.record_hash)
    if model_sha is None or not pages or 0 not in pages:
        raise ActivityCoverageError("coverage:first_page_missing")
    expected_offset, terminal = 0, False
    total = 0
    ordered: list[tuple[int, str]] = []
    while expected_offset in pages:
        limit, record_hash = pages.pop(expected_offset)
        # Length is reloaded via page metadata held by the record's source ID is unavailable;
        # use the offset advancement invariant below from the captured response count instead.
        ordered.append((expected_offset, record_hash))
        # `pages` stores only limit/hash; count is recovered in a dedicated mapping below.
        expected_offset += limit
    # Re-run using submitted offsets to avoid treating an unobserved next offset as complete.
    offsets = [offset for offset, _ in ordered]
    if pages or offsets != sorted(offsets):
        raise ActivityCoverageError("coverage:offset_gap_or_extra_page")
    # A terminal page cannot be inferred after dropping response sizes: retain them in source order.
    # The decoder intentionally requires callers to include an explicit empty/short terminal page.
    # See `_coverage_with_counts`, which performs that validation before this return.
    return _coverage_with_counts(records, wallet=wallet)


def _coverage_with_counts(records: Iterable[Any], *, wallet: str) -> ActivityCoverage:
    """The count-preserving implementation used by ``activity_coverage``."""
    rows: list[tuple[int, int, int, str, str]] = []
    for record in records:
        query, response = record.query, record.response
        rows.append((int(query["offset"]), int(query["limit"]), len(response), record.record_hash, record.model_sha))
    rows.sort()
    expected, total = 0, 0
    pages: list[tuple[int, str]] = []
    for index, (offset, limit, count, record_hash, model_sha) in enumerate(rows):
        if offset != expected or count > limit:
            raise ActivityCoverageError("coverage:offset_gap")
        pages.append((offset, record_hash))
        total += count
        expected += limit
        if count < limit:
            if index != len(rows) - 1:
                raise ActivityCoverageError("coverage:page_after_terminal")
            return ActivityCoverage(model_sha, wallet, tuple(pages), total)
    raise ActivityCoverageError("coverage:terminal_short_page_missing")
