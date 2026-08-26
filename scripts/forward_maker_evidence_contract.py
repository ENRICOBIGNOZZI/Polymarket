#!/usr/bin/env python3
"""Preserve causal maker PnL history while censoring legacy markout evidence.

The forward-maker probe historically substituted the final available book when a
45/60/300-second horizon was not actually observed.  PnL/fill sufficient
statistics from those sessions remain valid, but their stored markout aggregates
must not be mixed with sessions produced by the strict-horizon probe.

This module provides two contracts:

* ``compact_strict_session`` tags newly produced compact history and records the
  45-second markout sufficient statistic in addition to the existing 60/300s
  statistics.
* the CLI sanitizes old untagged markout fields before delegating all promotion
  gates/bootstrap inference to ``calibrate_forward_maker.py``.  It then augments
  the output with strict-only markout coverage.

No order submission or production mutation is performed here.
"""
from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

import calibrate_forward_maker as base

STRICT_MARKOUT_CONTRACT = "strict_horizon_observed_v1"
MARKOUT_HORIZONS = (45, 60, 300)


def _strict_contract(session: dict[str, Any]) -> bool:
    return str(session.get("markout_contract") or "") == STRICT_MARKOUT_CONTRACT


def _zero_legacy_markouts_in_summary(summary: dict[str, Any]) -> None:
    for horizon in (60, 300):
        summary[f"markout_{horizon}_weighted_sum"] = 0.0
        summary[f"markout_{horizon}_weight"] = 0.0
    summary["markout_45_weighted_sum"] = 0.0
    summary["markout_45_weight"] = 0.0


def sanitize_session_for_calibration(session: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy whose untrusted legacy markouts cannot enter inference."""
    cleaned = copy.deepcopy(session)
    if _strict_contract(cleaned):
        return cleaned

    summaries = cleaned.get("policy_summaries")
    if isinstance(summaries, list):
        for summary in summaries:
            if isinstance(summary, dict):
                _zero_legacy_markouts_in_summary(summary)

    # Raw legacy sessions are uncommon in bounded telemetry, but fail closed if
    # one is encountered: retain fills/PnL while removing horizon observations.
    results = cleaned.get("results")
    if isinstance(results, list):
        for row in results:
            if not isinstance(row, dict):
                continue
            for side in ("yes", "no"):
                leg = row.get(side)
                if not isinstance(leg, dict):
                    continue
                for horizon in MARKOUT_HORIZONS:
                    leg[f"markout_{horizon}_bid_per_share"] = None
    return cleaned


def _markout_sufficient_statistics(
    raw_session: dict[str, Any], horizon: int
) -> dict[str, tuple[float, float]]:
    by_policy: dict[str, tuple[float, float]] = {}
    running: dict[str, list[float]] = {}
    results = raw_session.get("results")
    if not isinstance(results, list):
        return by_policy
    key = f"markout_{horizon}_bid_per_share"
    for row in results:
        if not isinstance(row, dict):
            continue
        policy = str(row.get("policy") or "").strip()
        if not policy:
            continue
        bucket = running.setdefault(policy, [0.0, 0.0])
        for side in ("yes", "no"):
            leg = row.get(side)
            if not isinstance(leg, dict):
                continue
            shares = max(0.0, base.finite(leg.get("filled_shares")))
            markout = base.optional_finite(leg.get(key))
            if shares <= 0.0 or markout is None:
                continue
            bucket[0] += shares * markout
            bucket[1] += shares
    for policy, values in running.items():
        by_policy[policy] = (values[0], values[1])
    return by_policy


def compact_strict_session(raw_session: dict[str, Any]) -> dict[str, Any]:
    """Compact a session and make the strict-horizon evidence contract durable."""
    compact = base.compact_session(raw_session)
    compact["markout_contract"] = STRICT_MARKOUT_CONTRACT
    m45 = _markout_sufficient_statistics(raw_session, 45)
    summaries = compact.get("policy_summaries")
    if isinstance(summaries, list):
        for summary in summaries:
            if not isinstance(summary, dict):
                continue
            policy = str(summary.get("policy") or "")
            weighted_sum, weight = m45.get(policy, (0.0, 0.0))
            summary["markout_45_weighted_sum"] = weighted_sum
            summary["markout_45_weight"] = weight
    return compact


def _iter_policy_summaries(
    sessions: Iterable[dict[str, Any]],
) -> Iterable[tuple[dict[str, Any], dict[str, Any]]]:
    for session in sessions:
        summaries = session.get("policy_summaries")
        if not isinstance(summaries, list):
            continue
        for summary in summaries:
            if isinstance(summary, dict):
                yield session, summary


def augment_calibration_markouts(
    payload: dict[str, Any], sessions: list[dict[str, Any]], malformed_lines: int
) -> dict[str, Any]:
    """Add strict-only 45s coverage and make legacy exclusion auditable."""
    unique_sessions, _ = base.deduplicate_sessions(sessions)
    strict_sessions_by_policy: dict[str, int] = {}
    legacy_excluded_by_policy: dict[str, int] = {}
    m45_sum: dict[str, float] = {}
    m45_weight: dict[str, float] = {}

    for session, summary in _iter_policy_summaries(unique_sessions):
        policy = str(summary.get("policy") or "").strip()
        if not policy:
            continue
        if _strict_contract(session):
            strict_sessions_by_policy[policy] = strict_sessions_by_policy.get(policy, 0) + 1
            weight = max(0.0, base.finite(summary.get("markout_45_weight")))
            weighted_sum = base.finite(summary.get("markout_45_weighted_sum"))
            m45_sum[policy] = m45_sum.get(policy, 0.0) + weighted_sum
            m45_weight[policy] = m45_weight.get(policy, 0.0) + weight
        else:
            legacy_weight = max(
                0.0,
                base.finite(summary.get("markout_60_weight"))
                + base.finite(summary.get("markout_300_weight")),
            )
            if legacy_weight > 0.0:
                legacy_excluded_by_policy[policy] = legacy_excluded_by_policy.get(policy, 0) + 1

    reports = payload.get("by_policy")
    if isinstance(reports, dict):
        for policy, report in reports.items():
            if not isinstance(report, dict):
                continue
            weight45 = m45_weight.get(policy, 0.0)
            report["markout_contract"] = STRICT_MARKOUT_CONTRACT
            report["strict_markout_sessions"] = strict_sessions_by_policy.get(policy, 0)
            report["legacy_markout_sessions_excluded"] = legacy_excluded_by_policy.get(policy, 0)
            report["filled_share_weighted_markout_45_bid_per_share"] = (
                m45_sum.get(policy, 0.0) / weight45 if weight45 > 0.0 else None
            )
            report["markout_45_observed_filled_shares"] = weight45
            report["strict_markout_60_observed_filled_shares"] = max(
                0.0, base.finite(report.get("markout_60_observed_filled_shares"))
            )
            report["strict_markout_300_observed_filled_shares"] = max(
                0.0, base.finite(report.get("markout_300_observed_filled_shares"))
            )

    history = payload.get("history")
    if isinstance(history, dict):
        history["malformed_lines"] = malformed_lines
        history["strict_markout_sessions"] = sum(
            1 for session in unique_sessions if _strict_contract(session)
        )
        history["legacy_markout_sessions"] = sum(
            1 for session in unique_sessions if not _strict_contract(session)
        )

    payload["markout_evidence_contract"] = {
        "name": STRICT_MARKOUT_CONTRACT,
        "horizons_seconds": list(MARKOUT_HORIZONS),
        "legacy_markouts_excluded": True,
        "fills_and_pnl_from_legacy_sessions_retained": True,
        "rule": (
            "A markout contributes only when its compact session is tagged as "
            "strict-horizon evidence; untagged legacy 60/300s aggregates are censored."
        ),
    }
    return payload


def _write_jsonl(path: Path, sessions: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for session in sessions:
            handle.write(json.dumps(session, separators=(",", ":")) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args, remainder = parser.parse_known_args()

    sessions, malformed = base.load_history(args.history)
    sanitized = [sanitize_session_for_calibration(session) for session in sessions]

    with tempfile.TemporaryDirectory(prefix="forward-maker-strict-") as temporary:
        sanitized_path = Path(temporary) / "history.jsonl"
        _write_jsonl(sanitized_path, sanitized)
        command = [
            sys.executable,
            str(Path(__file__).with_name("calibrate_forward_maker.py")),
            "--history",
            str(sanitized_path),
            "--output",
            str(args.output),
            *remainder,
        ]
        subprocess.run(command, check=True)

    payload = json.loads(args.output.read_text(encoding="utf-8"))
    augment_calibration_markouts(payload, sessions, malformed)
    base.atomic_json(args.output, payload)
    strict_sessions = payload.get("history", {}).get("strict_markout_sessions", 0)
    print(
        "forward_maker_markout_contract"
        f" strict_sessions={strict_sessions}"
        f" legacy_excluded={payload.get('history', {}).get('legacy_markout_sessions', 0)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
