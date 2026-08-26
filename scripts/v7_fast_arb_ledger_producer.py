#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from v7_execution_ledger import (
    CanonicalLedgerWriter,
    LedgerEvent,
    canonical_ledger_path,
    load_events,
)

SESSION_SCHEMA = "polymarket_v7_fast_arb_ledger_session_v1"
SOURCE_NAME = "polymarket_fast_arb_shadow"
STRATEGY = "FAST_STRUCTURAL_ARB"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

BASE_FIELDS = {
    "observed_ts_ms",
    "kind",
    "id",
    "event_id",
    "hard_arbitrage",
    "executable",
    "risk_class",
    "reject_reason",
    "payoff_floor",
    "raw_edge_per_share",
    "net_edge_per_share",
    "executable_shares",
    "capital_required",
    "expected_profit",
    "exchange_ts_ms",
    "received_ts_ms",
    "decision_ts_ms",
    "feed_latency_ms",
    "legs",
}


class FastArbSourceError(ValueError):
    """Fast-arbitrage source data cannot satisfy the canonical V7 ledger contract."""


def _exact_sha(value: str, field: str = "model_sha") -> str:
    value = str(value).strip().lower()
    if not _SHA_RE.fullmatch(value):
        raise FastArbSourceError(f"{field}:not_exact_git_sha")
    return value


def _positive_int(row: dict[str, str], key: str, *, required: bool = True) -> int | None:
    raw = str(row.get(key) or "").strip()
    if not raw:
        if required:
            raise FastArbSourceError(f"{key}:missing")
        return None
    try:
        value = int(float(raw))
    except ValueError as exc:
        raise FastArbSourceError(f"{key}:not_integer") from exc
    if value <= 0:
        if required:
            raise FastArbSourceError(f"{key}:not_positive")
        return None
    return value


def _finite(row: dict[str, str], key: str, *, default: float | None = None) -> float | None:
    raw = str(row.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise FastArbSourceError(f"{key}:not_numeric") from exc
    if not math.isfinite(value):
        raise FastArbSourceError(f"{key}:not_finite")
    return value


def _boolean(row: dict[str, str], key: str) -> bool:
    raw = str(row.get(key) or "").strip().lower()
    if raw in {"1", "true", "yes"}:
        return True
    if raw in {"0", "false", "no"}:
        return False
    raise FastArbSourceError(f"{key}:not_boolean")


def _row_key(row: dict[str, str]) -> str:
    payload = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_legs(encoded: str) -> list[dict[str, Any]]:
    legs: list[dict[str, Any]] = []
    for index, chunk in enumerate(str(encoded).split("|")):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":", 6)
        if len(parts) != 7:
            raise FastArbSourceError(f"legs:{index}:invalid_shape")
        market_id, token_id, outcome, shares, average_price, fee_per_share, all_in_cost = parts
        if not market_id or not token_id or not outcome:
            raise FastArbSourceError(f"legs:{index}:missing_identity")
        try:
            shares_f = float(shares)
            average_price_f = float(average_price)
            fee_per_share_f = float(fee_per_share)
            all_in_cost_f = float(all_in_cost)
        except ValueError as exc:
            raise FastArbSourceError(f"legs:{index}:not_numeric") from exc
        values = (shares_f, average_price_f, fee_per_share_f, all_in_cost_f)
        if not all(math.isfinite(value) for value in values):
            raise FastArbSourceError(f"legs:{index}:not_finite")
        if shares_f <= 0.0 or not (0.0 <= average_price_f <= 1.0) or fee_per_share_f < 0.0:
            raise FastArbSourceError(f"legs:{index}:out_of_range")
        legs.append(
            {
                "market_id": market_id,
                "token_id": token_id,
                "outcome": outcome,
                "shares": shares_f,
                "average_price": average_price_f,
                "fee_per_share": fee_per_share_f,
                "all_in_cost": all_in_cost_f,
            }
        )
    if len(legs) < 2:
        raise FastArbSourceError("legs:structural_bundle_requires_two_or_more")
    return legs


def parse_leg_context(raw: str, legs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FastArbSourceError("leg_context_json:invalid_json") from exc
    if not isinstance(payload, list):
        raise FastArbSourceError("leg_context_json:not_array")
    by_token: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise FastArbSourceError(f"leg_context_json:{index}:not_object")
        token = str(item.get("token_id") or "").strip()
        snapshot = str(item.get("book_snapshot_id") or "").strip()
        if not token or not snapshot or token in by_token:
            raise FastArbSourceError(f"leg_context_json:{index}:identity")
        try:
            exchange = int(item["exchange_ts_ms"])
            receive = int(item["receive_ts_ms"])
        except (KeyError, TypeError, ValueError) as exc:
            raise FastArbSourceError(f"leg_context_json:{index}:clock") from exc
        if exchange <= 0 or receive <= 0 or receive < exchange:
            raise FastArbSourceError(f"leg_context_json:{index}:clock_order")
        by_token[token] = {
            "exchange_ts_ms": exchange,
            "receive_ts_ms": receive,
            "book_snapshot_id": snapshot,
        }
    expected = {str(leg["token_id"]) for leg in legs}
    if set(by_token) != expected:
        raise FastArbSourceError("leg_context_json:token_set_mismatch")
    return by_token


def checkout_sha(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise FastArbSourceError("checkout_sha:unavailable") from exc
    return _exact_sha(result.stdout.strip(), "checkout_sha")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}.{uuid.uuid4().hex}")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def begin_session(run_dir: Path, *, model_sha: str, started_ts_ms: int | None = None) -> Path:
    model_sha = _exact_sha(model_sha)
    started = int(started_ts_ms or time.time_ns() // 1_000_000)
    if started <= 0:
        raise FastArbSourceError("started_ts_ms:not_positive")
    manifest = {
        "schema": SESSION_SCHEMA,
        "session_id": uuid.uuid4().hex,
        "source": SOURCE_NAME,
        "model_sha": model_sha,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "started_ts_ms": started,
        "opportunities_file": "fast_arb_opportunities.csv",
        "status_file": "fast_arb_status.json",
    }
    path = Path(run_dir) / "fast_arb_ledger_session.json"
    _atomic_json(path, manifest)
    return path


def load_session(path: Path, *, expected_model_sha: str) -> dict[str, Any]:
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FastArbSourceError("session:unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != SESSION_SCHEMA:
        raise FastArbSourceError("session:schema")
    if manifest.get("source") != SOURCE_NAME:
        raise FastArbSourceError("session:source")
    if _exact_sha(str(manifest.get("model_sha") or "")) != _exact_sha(expected_model_sha):
        raise FastArbSourceError("session:mixed_sha")
    if manifest.get("paper_only") is not True or manifest.get("authenticated_execution") is not False:
        raise FastArbSourceError("session:safety_boundary")
    if manifest.get("real_order_submission") is not False:
        raise FastArbSourceError("session:real_order_submission")
    started = manifest.get("started_ts_ms")
    if isinstance(started, bool) or not isinstance(started, int) or started <= 0:
        raise FastArbSourceError("session:started_ts_ms")
    if manifest.get("opportunities_file") != "fast_arb_opportunities.csv":
        raise FastArbSourceError("session:opportunities_file")
    if manifest.get("status_file") != "fast_arb_status.json":
        raise FastArbSourceError("session:status_file")
    return manifest


def load_status(path: Path, *, session_started_ts_ms: int) -> dict[str, Any]:
    try:
        status = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FastArbSourceError("status:unreadable") from exc
    if not isinstance(status, dict):
        raise FastArbSourceError("status:not_object")
    if status.get("mode") != "shadow" or status.get("real_order_submission") is not False:
        raise FastArbSourceError("status:not_safe_shadow")
    timestamp = status.get("timestamp_ms")
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < session_started_ts_ms:
        raise FastArbSourceError("status:predates_session")
    return status


def read_rows(path: Path) -> list[dict[str, str]]:
    try:
        with Path(path).open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            fields = set(reader.fieldnames or [])
            missing = BASE_FIELDS - fields
            if missing:
                raise FastArbSourceError(f"opportunities:missing_field:{sorted(missing)[0]}")
            return [dict(row) for row in reader]
    except OSError as exc:
        raise FastArbSourceError("opportunities:unreadable") from exc


def _candidate_ready(
    row: dict[str, str],
    legs: list[dict[str, Any]],
    model_sha: str,
) -> tuple[bool, list[str], dict[str, dict[str, Any]] | None]:
    blockers: list[str] = []
    source_sha = str(row.get("model_sha") or "").strip()
    if source_sha:
        if _exact_sha(source_sha, "source_model_sha") != model_sha:
            raise FastArbSourceError("source_model_sha:mixed_sha")
    else:
        blockers.append("source_row_missing_model_sha")

    snapshot = str(row.get("book_snapshot_id") or "").strip()
    if not snapshot:
        blockers.append("missing_exact_book_snapshot_identity")

    context_raw = str(row.get("leg_context_json") or "").strip()
    context = None
    if not context_raw:
        blockers.append("missing_per_leg_clock_lineage")
    else:
        context = parse_leg_context(context_raw, legs)
        oldest_exchange = min(int(item["exchange_ts_ms"]) for item in context.values())
        latest_receive = max(int(item["receive_ts_ms"]) for item in context.values())
        bundle_exchange = _positive_int(row, "exchange_ts_ms", required=False)
        bundle_receive = _positive_int(row, "received_ts_ms", required=False)
        decision = _positive_int(row, "decision_ts_ms", required=False)
        if bundle_exchange != oldest_exchange:
            blockers.append("bundle_exchange_not_oldest_required_leg")
        if bundle_receive != latest_receive:
            blockers.append("bundle_receive_not_latest_required_leg")
        if decision is None or decision < latest_receive:
            blockers.append("decision_before_latest_required_leg_receive")

    return not blockers, blockers, context


def ingest_rows(
    rows: list[dict[str, str]],
    *,
    session: dict[str, Any],
    status: dict[str, Any],
    writer: CanonicalLedgerWriter,
    existing_row_keys: set[str] | None = None,
) -> dict[str, int]:
    model_sha = _exact_sha(str(session["model_sha"]))
    started = int(session["started_ts_ms"])
    status_ts = int(status["timestamp_ms"])
    existing = set(existing_row_keys or set())
    counts = {
        "source_rows": 0,
        "hard_structural_rows": 0,
        "opportunities_appended": 0,
        "canonical_candidates_appended": 0,
        "candidate_rows_blocked": 0,
        "duplicate_rows": 0,
        "pre_session_rows": 0,
        "non_hard_rows": 0,
    }

    for row in rows:
        counts["source_rows"] += 1
        observed = _positive_int(row, "observed_ts_ms")
        assert observed is not None
        if observed < started:
            counts["pre_session_rows"] += 1
            continue
        if observed > status_ts:
            raise FastArbSourceError("opportunity:after_status_snapshot")
        if not _boolean(row, "hard_arbitrage"):
            counts["non_hard_rows"] += 1
            continue
        counts["hard_structural_rows"] += 1
        source_key = _row_key(row)
        if source_key in existing:
            counts["duplicate_rows"] += 1
            continue

        opportunity_id = str(row.get("id") or "").strip()
        kind = str(row.get("kind") or "").strip()
        if not opportunity_id or not kind:
            raise FastArbSourceError("opportunity:missing_identity")
        event_id = str(row.get("event_id") or "").strip() or None
        exchange = _positive_int(row, "exchange_ts_ms", required=False)
        receive = _positive_int(row, "received_ts_ms", required=False)
        decision = _positive_int(row, "decision_ts_ms", required=False)
        if receive is not None and decision is not None and decision < receive:
            raise FastArbSourceError("opportunity:decision_before_receive")
        if decision is not None and observed < decision:
            raise FastArbSourceError("opportunity:observed_before_decision")

        legs = parse_legs(str(row.get("legs") or ""))
        executable = _boolean(row, "executable")
        candidate_ready, blockers, leg_context = _candidate_ready(row, legs, model_sha)
        bundle_id = f"fast:{opportunity_id}:{observed}"
        common_metadata = {
            "source": "fast_arb_opportunities.csv",
            "source_row_key": source_key,
            "source_session_id": session["session_id"],
            "kind": kind,
            "hard_arbitrage": True,
            "source_executable": executable,
            "risk_class": str(row.get("risk_class") or ""),
            "reject_reason": str(row.get("reject_reason") or ""),
            "payoff_floor": _finite(row, "payoff_floor", default=0.0),
            "raw_edge_per_share": _finite(row, "raw_edge_per_share", default=0.0),
            "capital_required": _finite(row, "capital_required", default=0.0),
            "candidate_contract_ready": candidate_ready,
            "candidate_blockers": blockers,
            "per_leg_context_required": True,
            "bundle_clock_semantics": "oldest_leg_exchange/latest_leg_receive",
            "promotion_eligible": False,
        }
        writer.append(
            LedgerEvent(
                event_type="OPPORTUNITY",
                strategy=STRATEGY,
                model_sha=model_sha,
                opportunity_id=bundle_id,
                bundle_id=bundle_id,
                event_id=event_id,
                decision_ts_ms=decision,
                exchange_ts_ms=exchange,
                receive_ts_ms=receive,
                predicted_alpha=_finite(row, "net_edge_per_share", default=0.0),
                expected_ev=_finite(row, "expected_profit", default=0.0),
                intended_size=_finite(row, "executable_shares", default=0.0),
                metadata=common_metadata,
            )
        )
        counts["opportunities_appended"] += 1
        existing.add(source_key)

        if not executable:
            continue
        if (
            not candidate_ready
            or leg_context is None
            or exchange is None
            or receive is None
            or decision is None
        ):
            counts["candidate_rows_blocked"] += 1
            continue

        snapshot = str(row["book_snapshot_id"]).strip()
        candidate_id = f"{bundle_id}:candidate"
        writer.append(
            LedgerEvent(
                event_type="CANDIDATE",
                strategy=STRATEGY,
                model_sha=model_sha,
                opportunity_id=bundle_id,
                candidate_id=candidate_id,
                bundle_id=bundle_id,
                event_id=event_id,
                decision_ts_ms=decision,
                exchange_ts_ms=exchange,
                receive_ts_ms=receive,
                book_snapshot_id=snapshot,
                predicted_alpha=_finite(row, "net_edge_per_share", default=0.0),
                expected_ev=_finite(row, "expected_profit", default=0.0),
                intended_action="STRUCTURAL_ARB_PAPER_CANDIDATE",
                intended_size=_finite(row, "executable_shares", default=0.0),
                metadata={**common_metadata, "bundle_candidate": True},
            )
        )
        counts["canonical_candidates_appended"] += 1

        for index, leg in enumerate(legs):
            context = leg_context[str(leg["token_id"])]
            writer.append(
                LedgerEvent(
                    event_type="CANDIDATE",
                    strategy=STRATEGY,
                    model_sha=model_sha,
                    opportunity_id=bundle_id,
                    candidate_id=f"{candidate_id}:leg:{index}",
                    bundle_id=bundle_id,
                    leg_id=f"{bundle_id}:leg:{index}",
                    market_id=str(leg["market_id"]),
                    event_id=event_id,
                    token_id=str(leg["token_id"]),
                    decision_ts_ms=decision,
                    exchange_ts_ms=int(context["exchange_ts_ms"]),
                    receive_ts_ms=int(context["receive_ts_ms"]),
                    book_snapshot_id=str(context["book_snapshot_id"]),
                    side="BUY",
                    intended_action="CROSS_DEPTH_PAPER_CANDIDATE",
                    intended_size=float(leg["shares"]),
                    metadata={
                        **common_metadata,
                        "bundle_candidate": False,
                        "leg_index": index,
                        "outcome": str(leg["outcome"]),
                        "depth_walk_average_price": float(leg["average_price"]),
                        "fee_per_share": float(leg["fee_per_share"]),
                        "all_in_cost": float(leg["all_in_cost"]),
                    },
                )
            )
            counts["canonical_candidates_appended"] += 1

    return counts


def ingest_run(run_dir: Path, *, repo_root: Path, ledger_path: Path | None = None) -> dict[str, int]:
    model_sha = checkout_sha(repo_root)
    run_dir = Path(run_dir)
    session = load_session(run_dir / "fast_arb_ledger_session.json", expected_model_sha=model_sha)
    status = load_status(
        run_dir / "fast_arb_status.json",
        session_started_ts_ms=int(session["started_ts_ms"]),
    )
    rows = read_rows(run_dir / "fast_arb_opportunities.csv")
    target = Path(ledger_path) if ledger_path is not None else canonical_ledger_path(run_dir)
    existing: set[str] = set()
    if target.exists():
        for event in load_events(target, expected_model_sha=model_sha):
            if event.strategy == STRATEGY:
                key = event.metadata.get("source_row_key")
                if isinstance(key, str) and key:
                    existing.add(key)
    with CanonicalLedgerWriter(
        target,
        writer_id="v7-fast-structural-ledger-producer",
        model_sha=model_sha,
    ) as writer:
        return ingest_rows(
            rows,
            session=session,
            status=status,
            writer=writer,
            existing_row_keys=existing,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="V7 Fast Structural Arb -> canonical execution ledger")
    sub = parser.add_subparsers(dest="command", required=True)

    begin = sub.add_parser(
        "begin",
        help="bind a fresh Fast Arb source session to the exact checkout SHA",
    )
    begin.add_argument("--run-dir", required=True, type=Path)
    begin.add_argument("--repo-root", default=Path("."), type=Path)

    ingest = sub.add_parser(
        "ingest",
        help="append fail-closed Fast Arb opportunity/candidate evidence",
    )
    ingest.add_argument("--run-dir", required=True, type=Path)
    ingest.add_argument("--repo-root", default=Path("."), type=Path)
    ingest.add_argument("--ledger", type=Path)

    args = parser.parse_args()
    if args.command == "begin":
        model_sha = checkout_sha(args.repo_root)
        path = begin_session(args.run_dir, model_sha=model_sha)
        print(json.dumps({"session": str(path), "model_sha": model_sha}, sort_keys=True))
        return 0

    summary = ingest_run(args.run_dir, repo_root=args.repo_root, ledger_path=args.ledger)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
