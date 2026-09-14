#!/usr/bin/env python3
"""Forward-window evaluator with source-level selector authority proof.

This wraps `v7_maker_forward_window_evaluator` and proves every canonical Maker
order against the exact reward-selection snapshot that generated its opportunity.
Copied ORDER_SUBMITTED provenance is never trusted as proof: a fresh proof is
reconstructed from the selector event log and injected only into an in-memory
copy before the frozen economic evaluator runs. The ledger is never mutated.
"""
from __future__ import annotations

import argparse
import copy
import gzip
import json
import pathlib
import sys
import time
from typing import Any, Iterable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import v7_maker_forward_window_evaluator as base

SCHEMA = "polymarket_v7_maker_forward_window_report_v2"


def evidence_files(paths: Iterable[pathlib.Path]) -> list[pathlib.Path]:
    output: set[pathlib.Path] = set()
    for raw in paths:
        path = pathlib.Path(raw)
        if path.is_file():
            output.add(path)
            if path.name.endswith(".jsonl"):
                output.update(
                    item for item in path.parent.glob(path.name + ".segment-*.jsonl.gz")
                    if item.is_file()
                )
        elif path.exists():
            output.update(item for item in path.rglob("*.jsonl") if item.is_file())
            output.update(item for item in path.rglob("*.jsonl.gz") if item.is_file())
    return sorted(output)


def iter_rows(path: pathlib.Path):
    opener = gzip.open if path.name.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    yield value
    except OSError:
        return


def selection_index(paths: Iterable[pathlib.Path], code_sha: str) -> dict[int, dict[str, Any]]:
    index: dict[int, dict[str, Any]] = {}
    ambiguous: set[int] = set()
    for path in evidence_files(paths):
        for row in iter_rows(path):
            timestamp = int(base.number(row.get("timestamp_ms"), 0))
            if timestamp <= 0:
                continue
            if (
                row.get("model_sha") != code_sha
                or row.get("paper_only") is not True
                or row.get("authenticated_execution") is not False
                or row.get("real_order_submission") is not False
                or not isinstance(row.get("markets"), list)
            ):
                continue
            # Once a timestamp has conflicting canonical payloads it remains
            # unusable forever, even if a later row happens to match one side.
            # Exact duplicates are harmless and retain the unique payload.
            if timestamp in ambiguous:
                continue
            if timestamp in index:
                if base.canonical_json(index[timestamp]) != base.canonical_json(row):
                    index.pop(timestamp, None)
                    ambiguous.add(timestamp)
                continue
            index[timestamp] = row
    return index


def envelope(order: dict[str, Any]) -> dict[str, Any]:
    meta = base.metadata(order)
    value = meta.get("opportunity_envelope")
    return value if isinstance(value, dict) else {}


def placement_action(env: dict[str, Any]) -> str:
    reasons = env.get("reasons") if isinstance(env.get("reasons"), list) else []
    actions = {
        str(reason)[10:].upper()
        for reason in reasons
        if str(reason).startswith("PLACEMENT_")
    }
    return actions.pop() if len(actions) == 1 else ""


def quote_side(env: dict[str, Any]) -> str:
    plan = env.get("execution_plan") if isinstance(env.get("execution_plan"), dict) else {}
    legs = plan.get("legs") if isinstance(plan.get("legs"), list) else []
    if len(legs) != 1 or not isinstance(legs[0], dict):
        return ""
    return str(legs[0].get("side") or "").upper()


def source_timestamp_ms(env: dict[str, Any]) -> int:
    stamps = env.get("source_event_timestamps_ns")
    if not isinstance(stamps, list) or len(stamps) != 1:
        return 0
    stamp = int(base.number(stamps[0], 0))
    return stamp // 1_000_000 if stamp > 0 and stamp % 1_000_000 == 0 else 0


def prove_order(
    order: dict[str, Any], snapshots: dict[int, dict[str, Any]], code_sha: str,
) -> tuple[dict[str, Any] | None, str]:
    env = envelope(order)
    timestamp = source_timestamp_ms(env)
    if timestamp <= 0:
        return None, "MISSING_EXACT_SELECTOR_TIMESTAMP"
    snapshot = snapshots.get(timestamp)
    if snapshot is None:
        return None, "SELECTOR_SNAPSHOT_NOT_FOUND_OR_AMBIGUOUS"
    if snapshot.get("model_sha") != code_sha:
        return None, "SELECTOR_SHA_MISMATCH"

    market_id = str(env.get("market_id") or order.get("market_id") or "")
    token_id = str(env.get("contract_id") or order.get("token_id") or "")
    outcome = str(env.get("side") or base.metadata(order).get("outcome") or "").upper()
    action = placement_action(env)
    side = quote_side(env)
    if not all((market_id, token_id, outcome, action, side)):
        return None, "INCOMPLETE_OPPORTUNITY_IDENTITY"

    market_matches = [
        row for row in snapshot.get("markets", [])
        if isinstance(row, dict) and str(row.get("market_id") or "") == market_id
    ]
    if len(market_matches) != 1:
        return None, "SELECTOR_MARKET_IDENTITY_NOT_UNIQUE"
    market = market_matches[0]
    cells = [
        cell for cell in market.get("authorized_execution_cells", [])
        if isinstance(cell, dict)
        and str(cell.get("token_id") or "") == token_id
        and str(cell.get("outcome") or "").upper() == outcome
        and str(cell.get("quote_side") or "").upper() == side
        and str(cell.get("action") or "").upper() == action
    ]
    if len(cells) != 1:
        return None, "SELECTOR_EXECUTION_CELL_NOT_UNIQUE"
    cell = cells[0]
    if str(cell.get("authority_basis") or "") != base.REQUIRED_BASIS:
        return None, "NON_FRESH_OPPOSITE_FLOW_AUTHORITY"

    quotes = [
        quote for quote in market.get("quote_opportunities", [])
        if isinstance(quote, dict)
        and str(quote.get("token_id") or "") == token_id
        and str(quote.get("outcome") or "").upper() == outcome
        and str(quote.get("quote_side") or "").upper() == side
    ]
    if len(quotes) != 1:
        return None, "SELECTOR_QUOTE_IDENTITY_NOT_UNIQUE"
    quote = quotes[0]
    if quote.get("opposite_flow_is_fresh") is not True:
        return None, "SELECTOR_FLOW_NOT_FRESH"

    cell_fill = base.number(cell.get("projected_fill_probability"))
    quote_fill = base.number(quote.get(
        "projected_improve1_fill_probability" if action == "IMPROVE1"
        else "projected_join_fill_probability"
    ))
    if not (base.math.isfinite(cell_fill) and base.math.isfinite(quote_fill)):
        return None, "PROJECTED_FILL_EVIDENCE_MISSING"
    if abs(cell_fill - quote_fill) > 1e-9:
        return None, "CELL_QUOTE_FILL_PROBABILITY_MISMATCH"

    proof = {
        "authority_basis": base.REQUIRED_BASIS,
        "flow_source": str(quote.get("flow_source") or snapshot.get("recent_flow_source") or snapshot.get("source") or ""),
        "opposite_flow_is_fresh": True,
        "opposite_prints_2m": int(base.number(quote.get("opposite_prints_2m"), 0)),
        "opposite_prints_10m": int(base.number(quote.get("opposite_prints_10m"), 0)),
        "last_opposite_flow_age_ms": int(base.number(quote.get("last_opposite_flow_age_ms"), -1)),
        "projected_fill_probability": cell_fill,
        "selector_timestamp_ms": timestamp,
        "selector_market_id": market_id,
        "selector_token_id": token_id,
        "selector_action": action,
        "selector_quote_side": side,
        "proof_source": "CANONICAL_REWARD_SELECTION_EVENT_LOG",
    }
    return proof, "PROVEN"


def inject_source_proofs(
    ledger: list[dict[str, Any]], manifest: dict[str, Any],
    snapshots: dict[int, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    code_sha = str(manifest["code_sha"])
    start, end = int(manifest["window_start_ms"]), int(manifest["window_end_ms"])
    output = copy.deepcopy(ledger)
    proven = failed = existing_seen = existing_replaced = 0
    failures: dict[str, str] = {}
    for row in output:
        if (
            not base.canonical_maker(row, code_sha)
            or row.get("event_type") != "ORDER_SUBMITTED"
            or not start <= base.order_time(row) < end
        ):
            continue
        meta = base.metadata(row)
        alpha = meta.get("execution_alpha") if isinstance(meta.get("execution_alpha"), dict) else None
        if alpha is None:
            failures[str(row.get("order_id") or "")] = "MISSING_EXECUTION_ALPHA"
            failed += 1
            continue
        if isinstance(alpha.get("flow_provenance"), dict):
            existing_seen += 1
        # Existing copied provenance is deliberately discarded. The v2 audit
        # succeeds only if the canonical selector source can independently
        # reconstruct the authority for this exact order.
        alpha.pop("flow_provenance", None)
        proof, state = prove_order(row, snapshots, code_sha)
        if proof is None:
            failures[str(row.get("order_id") or "")] = state
            failed += 1
            continue
        alpha["flow_provenance"] = proof
        proven += 1
        existing_replaced += int(existing_seen > existing_replaced)
    return output, {
        "proof_method": "EXACT_SELECTOR_EVENT_LOG_JOIN",
        "orders_proven_from_source": proven,
        "orders_with_existing_copied_provenance": existing_seen,
        "orders_existing_provenance_replaced_by_source_proof": existing_replaced,
        "orders_unproven": failed,
        "unproven_order_reasons": failures,
        "selector_snapshots_indexed": len(snapshots),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    parser.add_argument("--ledger", type=pathlib.Path)
    parser.add_argument("--markout-root", type=pathlib.Path)
    parser.add_argument("--selection-evidence", type=pathlib.Path, action="append", default=[])
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--now-ms", type=int)
    parser.add_argument("--bootstrap-draws", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=140926)
    args = parser.parse_args()
    manifest = base.validate_manifest(base.read_json(args.manifest))
    if args.validate_only:
        result = {
            "schema": SCHEMA,
            "state": "VALIDATED_PRE_FORWARD_EVALUATOR_V2",
            "experiment_id": manifest.get("experiment_id"),
            "code_sha": manifest["code_sha"],
            "manifest_sha256": manifest.get("manifest_sha256") or base.canonical_hash(manifest, "manifest_sha256"),
            "authority_proof": "EXACT_SELECTOR_EVENT_LOG_JOIN",
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "automatic_promotion": False,
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    now_ms = int(args.now_ms if args.now_ms is not None else time.time_ns() // 1_000_000)
    if now_ms < int(manifest["window_end_ms"]):
        result = {
            "schema": SCHEMA,
            "state": "IN_PROGRESS_NO_INTERIM_ENDPOINT_LOOK",
            "experiment_id": manifest.get("experiment_id"),
            "code_sha": manifest["code_sha"],
            "window_end_ms": manifest["window_end_ms"],
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "automatic_promotion": False,
        }
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 3
    if args.ledger is None or args.markout_root is None or not args.selection_evidence:
        raise SystemExit("--ledger, --markout-root and --selection-evidence are required after window close")
    snapshots = selection_index(args.selection_evidence, str(manifest["code_sha"]))
    ledger = base.ledger_rows(args.ledger)
    proven_ledger, authority_audit = inject_source_proofs(ledger, manifest, snapshots)
    result = base.evaluate(
        manifest,
        proven_ledger,
        base.load_markouts(args.markout_root, str(manifest["code_sha"])),
        bootstrap_draws=args.bootstrap_draws,
        seed=args.seed,
    )
    result["schema"] = SCHEMA
    result["authority_source_audit"] = authority_audit
    result["generated_at_ms"] = now_ms
    if authority_audit["orders_unproven"] > 0:
        result["state"] = "HARD_CORRECTNESS_FAILURE"
    core = dict(result)
    core.pop("report_sha256", None)
    result["report_sha256"] = base.canonical_hash(core)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
