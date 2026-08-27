#!/usr/bin/env python3
"""Normalize V7 research outputs into exact-SHA canonical model intents.

This is the only bridge from LF/PCA/Ranking research reports into the V7
execution plane.  It is intentionally fail-closed: research outputs remain
non-executable until their strategy-specific causal/universe gates are proved.
The router never manufactures fills or PnL and never converts relative ranking
into an absolute single-leg bet.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import v7_execution_ledger as ledger

SCHEMA = "polymarket_v7_model_intents_v1"
FAMILIES = {"local_factor", "pca", "ranking"}


class IntentContractError(ValueError):
    pass


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _load_json(path: Path) -> Any:
    value = json.loads(path.read_text(encoding="utf-8"))
    return value


def _candidate_id(model_sha: str, family: str, horizon_seconds: int, legs: Iterable[dict[str, Any]]) -> str:
    identity = [
        f"{_text(leg.get('market_id'))}:{_text(leg.get('side'))}:{_finite(leg.get('weight')) or 0.0:.12g}"
        for leg in legs
    ]
    raw = "|".join([model_sha, family, str(horizon_seconds), *identity])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class ModelIntent:
    candidate_id: str
    model_sha: str
    family: str
    horizon_seconds: int
    decision_ts_ms: int
    semantics: str
    legs: tuple[dict[str, Any], ...]
    predicted_edge: float | None
    economic_score: float | None
    executable: bool
    blockers: tuple[str, ...]
    provenance: dict[str, Any]

    def validate(self) -> None:
        if self.family not in FAMILIES:
            raise IntentContractError("unknown_family")
        if len(self.model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in self.model_sha.lower()):
            raise IntentContractError("invalid_model_sha")
        if self.horizon_seconds <= 0 or self.decision_ts_ms <= 0:
            raise IntentContractError("invalid_time_contract")
        if not self.legs:
            raise IntentContractError("missing_legs")
        for leg in self.legs:
            if not _text(leg.get("market_id")) or _text(leg.get("side")).upper() not in {"YES", "NO"}:
                raise IntentContractError("invalid_leg_identity")
            weight = _finite(leg.get("weight"))
            if weight is None or weight <= 0.0:
                raise IntentContractError("invalid_leg_weight")
        if self.executable and self.blockers:
            raise IntentContractError("executable_intent_has_blockers")
        if self.family == "ranking":
            if self.semantics != "relative_top_bottom_pair" or len(self.legs) != 2:
                raise IntentContractError("ranking_must_remain_relative_pair")
            sides = tuple(_text(leg.get("side")).upper() for leg in self.legs)
            if sides != ("YES", "NO"):
                raise IntentContractError("ranking_absolute_mapping_forbidden")


def _base_report_gate(report: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if report.get("paper_only") is not True:
        blockers.append("paper_only_required")
    if report.get("research_only") is not True:
        blockers.append("research_only_source_required")
    if report.get("live_intents_enabled") is not False:
        blockers.append("research_source_live_intents_must_be_disabled")
    if int(report.get("submitted_orders") or 0) != 0:
        blockers.append("research_source_must_not_submit_orders")
    return blockers


def local_factor_intents(report: dict[str, Any], model_sha: str) -> list[ModelIntent]:
    blockers0 = _base_report_gate(report)
    if report.get("current_residual_reconstructed_from_frozen_controls") is not True:
        blockers0.append("current_residual_not_reconstructed")
    book_contract = report.get("current_book_snapshot_contract")
    if not isinstance(book_contract, dict) or book_contract.get("required") is not True:
        blockers0.append("causal_book_snapshot_contract_missing")
    elif int(book_contract.get("guard_rejections") or 0) > 0:
        blockers0.append("causal_book_snapshot_rejections_present")
    if report.get("survivorship_safe") is not True:
        blockers0.append("point_in_time_universe_not_validated")

    decision_ts_ms = int(report.get("timestamp") or 0) * 1000
    out: list[ModelIntent] = []
    for row in report.get("signals") or []:
        if not isinstance(row, dict):
            continue
        a, b = _text(row.get("market_a")), _text(row.get("market_b"))
        side_a, side_b = _text(row.get("side_a")).upper(), _text(row.get("side_b")).upper()
        wa, wb = _finite(row.get("weight_a")), _finite(row.get("weight_b"))
        horizon = int(row.get("hold_seconds") or 0)
        if not a or not b or a == b or side_a not in {"YES", "NO"} or side_b not in {"YES", "NO"} or wa is None or wb is None or wa <= 0 or wb <= 0 or horizon <= 0:
            continue
        legs = (
            {"role": "a", "market_id": a, "side": side_a, "weight": wa},
            {"role": "b", "market_id": b, "side": side_b, "weight": wb},
        )
        cid = _candidate_id(model_sha, "local_factor", horizon, legs)
        intent = ModelIntent(
            candidate_id=cid, model_sha=model_sha, family="local_factor",
            horizon_seconds=horizon, decision_ts_ms=decision_ts_ms,
            semantics="pair_residual_convergence", legs=legs,
            predicted_edge=None, economic_score=None,
            executable=not blockers0, blockers=tuple(sorted(set(blockers0))),
            provenance={
                "cluster": row.get("cluster"), "pvalue": row.get("pvalue"),
                "current_residual_z_a": row.get("current_residual_z_a"),
                "current_residual_z_b": row.get("current_residual_z_b"),
                "latest_completed_bucket_end_ts": row.get("latest_completed_bucket_end_ts"),
                "exchange_ts_ms": row.get("exchange_ts_ms"),
                "receive_ts_ms": row.get("receive_ts_ms"),
                "book_snapshot_id": row.get("book_snapshot_id"),
            },
        )
        intent.validate(); out.append(intent)
    return out


def pca_intents(report: dict[str, Any], model_sha: str) -> list[ModelIntent]:
    blockers0 = _base_report_gate(report)
    if report.get("single_leg_only") is not True or report.get("hedge_legs_allowed") is not False:
        blockers0.append("pca_single_leg_contract_invalid")
    if report.get("total_single_leg_forecast_risk") is not True:
        blockers0.append("pca_total_single_leg_risk_missing")
    if report.get("survivorship_safe") is not True:
        blockers0.append("point_in_time_universe_not_validated")
    decision_ts_ms = int(report.get("timestamp") or 0) * 1000
    out: list[ModelIntent] = []
    for horizon_row in report.get("horizons") or []:
        if not isinstance(horizon_row, dict):
            continue
        expected_horizon = int(horizon_row.get("horizon_minutes") or 0) * 60
        for row in horizon_row.get("shadow_candidates") or []:
            if not isinstance(row, dict):
                continue
            market_id, event_id = _text(row.get("market_id")), _text(row.get("event_id"))
            side = _text(row.get("side")).upper()
            horizon = int(row.get("horizon_seconds") or 0)
            edge, score = _finite(row.get("net_edge")), _finite(row.get("economic_score"))
            if not market_id or side not in {"YES", "NO"} or horizon <= 0 or horizon != expected_horizon or edge is None or score is None or edge <= 0 or score <= 0:
                continue
            legs = ({"role": "single", "market_id": market_id, "event_id": event_id, "side": side, "weight": 1.0, "entry_price": row.get("entry_price")},)
            cid = _candidate_id(model_sha, "pca", horizon, legs)
            intent = ModelIntent(
                candidate_id=cid, model_sha=model_sha, family="pca",
                horizon_seconds=horizon, decision_ts_ms=decision_ts_ms,
                semantics="single_leg_residual_stat_arb", legs=legs,
                predicted_edge=edge, economic_score=score,
                executable=not blockers0, blockers=tuple(sorted(set(blockers0))),
                provenance={
                    "predicted_logit_move": row.get("predicted_logit_move"),
                    "predicted_yes_probability": row.get("predicted_yes_probability"),
                    "uncertainty_penalty": row.get("uncertainty_penalty"),
                    "gross_markout": row.get("gross_markout"),
                    "exchange_ts_ms": row.get("exchange_ts_ms"),
                    "receive_ts_ms": row.get("receive_ts_ms"),
                    "book_snapshot_id": row.get("book_snapshot_id"),
                },
            )
            intent.validate(); out.append(intent)
    return out


def ranking_intents(report: dict[str, Any], pairs: list[Any], model_sha: str) -> list[ModelIntent]:
    blockers0 = _base_report_gate(report)
    if report.get("relative_pair_only") is not True or report.get("absolute_single_leg_mapping_disabled") is not True:
        blockers0.append("relative_pair_contract_invalid")
    if report.get("pool_evidence_across_horizons") is not False:
        blockers0.append("ranking_horizon_pooling_forbidden")
    if report.get("frozen_holdout_fit_validated") is not True:
        blockers0.append("frozen_holdout_fit_not_validated")
    if report.get("point_in_time_universe_validated") is not True or report.get("survivorship_safe") is not True:
        blockers0.append("point_in_time_universe_not_validated")
    decision_ts_ms = int(report.get("timestamp") or 0) * 1000
    out: list[ModelIntent] = []
    for row in pairs:
        if not isinstance(row, dict):
            continue
        top, bottom = _text(row.get("top_market_id")), _text(row.get("bottom_market_id"))
        if not top or not bottom or top == bottom:
            continue
        top_side, bottom_side = _text(row.get("top_side")).upper(), _text(row.get("bottom_side")).upper()
        horizon = int(row.get("horizon_seconds") or 0)
        wt, wb = _finite(row.get("top_shares_per_pair_dollar")), _finite(row.get("bottom_shares_per_pair_dollar"))
        edge, score = _finite(row.get("completed_pair_net_edge")), _finite(row.get("economic_score"))
        if (top_side, bottom_side) != ("YES", "NO") or horizon <= 0 or wt is None or wb is None or wt <= 0 or wb <= 0 or edge is None or score is None or edge <= 0 or score <= 0:
            continue
        legs = (
            {"role": "top", "market_id": top, "event_id": _text(row.get("top_event_id")), "side": "YES", "weight": wt},
            {"role": "bottom", "market_id": bottom, "event_id": _text(row.get("bottom_event_id")), "side": "NO", "weight": wb},
        )
        cid = _candidate_id(model_sha, "ranking", horizon, legs)
        intent = ModelIntent(
            candidate_id=cid, model_sha=model_sha, family="ranking",
            horizon_seconds=horizon, decision_ts_ms=decision_ts_ms,
            semantics="relative_top_bottom_pair", legs=legs,
            predicted_edge=edge, economic_score=score,
            executable=not blockers0, blockers=tuple(sorted(set(blockers0))),
            provenance={
                "predicted_relative_logit_spread": row.get("predicted_relative_logit_spread"),
                "common_logit_delta_per_pair_dollar": row.get("common_logit_delta_per_pair_dollar"),
                "max_pair_notional": row.get("max_pair_notional"),
                "exchange_ts_ms": row.get("exchange_ts_ms"),
                "receive_ts_ms": row.get("receive_ts_ms"),
                "book_snapshot_id": row.get("book_snapshot_id"),
            },
        )
        intent.validate(); out.append(intent)
    return out


def _causal_candidate_clock(intent: ModelIntent) -> tuple[int, int, str] | None:
    try:
        exchange_ts_ms = int(intent.provenance.get("exchange_ts_ms") or 0)
        receive_ts_ms = int(intent.provenance.get("receive_ts_ms") or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    snapshot_id = _text(intent.provenance.get("book_snapshot_id"))
    if exchange_ts_ms <= 0 or receive_ts_ms <= 0 or not snapshot_id:
        return None
    if exchange_ts_ms > receive_ts_ms or receive_ts_ms > intent.decision_ts_ms:
        return None
    return exchange_ts_ms, receive_ts_ms, snapshot_id


def write_candidate_events(writer: ledger.CanonicalLedgerWriter, intents: list[ModelIntent]) -> int:
    """Persist research opportunities; emit CANDIDATE only with causal book identity.

    The canonical ledger deliberately requires exchange/receive/decision clocks and
    a snapshot id for CANDIDATE.  A research intent that is blocked, or an otherwise
    eligible intent whose causal snapshot did not survive into the router, remains
    an OPPORTUNITY.  The router never fabricates timestamps to cross this boundary.
    """
    written = 0
    for intent in intents:
        intent.validate()
        market_ids = [str(leg["market_id"]) for leg in intent.legs]
        event_ids = [_text(leg.get("event_id")) for leg in intent.legs if _text(leg.get("event_id"))]
        required = [
            {"leg_id": str(index), "market_id": str(leg["market_id"]), "side": str(leg["side"]), "target_weight": float(leg["weight"])}
            for index, leg in enumerate(intent.legs)
        ]
        clock = _causal_candidate_clock(intent) if intent.executable else None
        candidate_ready = intent.executable and clock is not None
        router_blockers = list(intent.blockers)
        if intent.executable and clock is None:
            router_blockers.append("causal_book_clock_missing_at_router")
        exchange_ts_ms, receive_ts_ms, snapshot_id = clock if clock is not None else (None, None, None)
        writer.append(ledger.LedgerEvent(
            event_type="CANDIDATE" if candidate_ready else "OPPORTUNITY",
            strategy=intent.family,
            model_sha=intent.model_sha,
            opportunity_id=None if candidate_ready else intent.candidate_id,
            candidate_id=intent.candidate_id if candidate_ready else None,
            bundle_id=intent.candidate_id if candidate_ready and len(intent.legs) > 1 else None,
            market_id=market_ids[0] if len(market_ids) == 1 else "|".join(market_ids),
            event_id=event_ids[0] if len(set(event_ids)) == 1 else "|".join(event_ids),
            decision_ts_ms=intent.decision_ts_ms,
            exchange_ts_ms=exchange_ts_ms,
            receive_ts_ms=receive_ts_ms,
            book_snapshot_id=snapshot_id,
            predicted_alpha=intent.predicted_edge,
            expected_ev=intent.economic_score,
            intended_action="PAPER_EXECUTION_ELIGIBLE" if candidate_ready else "RESEARCH_OPPORTUNITY",
            metadata={
                "model_family": intent.family,
                "horizon_seconds": intent.horizon_seconds,
                "intent_semantics": intent.semantics,
                "joint_target_legs": required if len(required) > 1 else None,
                "legs": list(intent.legs),
                "model_execution_eligible": intent.executable,
                "execution_eligible": candidate_ready,
                "blockers": sorted(set(router_blockers)),
                "provenance": intent.provenance,
            },
        ))
        written += 1
    return written


def route(family: str, report_path: Path, *, model_sha: str, pairs_path: Path | None = None) -> list[ModelIntent]:
    report = _load_json(report_path)
    if not isinstance(report, dict):
        raise IntentContractError("report_not_object")
    if family == "local_factor":
        return local_factor_intents(report, model_sha)
    if family == "pca":
        return pca_intents(report, model_sha)
    if family == "ranking":
        if pairs_path is None:
            raise IntentContractError("ranking_pairs_required")
        pairs = _load_json(pairs_path)
        if not isinstance(pairs, list):
            raise IntentContractError("ranking_pairs_not_list")
        return ranking_intents(report, pairs, model_sha)
    raise IntentContractError("unknown_family")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family", choices=sorted(FAMILIES), required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--pairs", type=Path)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ledger", type=Path)
    args = parser.parse_args(argv)
    intents = route(args.family, args.report, model_sha=args.model_sha, pairs_path=args.pairs)
    payload = {
        "schema": SCHEMA, "model_sha": args.model_sha, "family": args.family,
        "paper_only": True, "authenticated_execution": False,
        "intents": [asdict(intent) for intent in intents],
        "execution_eligible": sum(1 for intent in intents if intent.executable),
        "blocked": sum(1 for intent in intents if not intent.executable),
    }
    _atomic_json(args.output, payload)
    if args.ledger:
        with ledger.CanonicalLedgerWriter(args.ledger, writer_id="v7-model-intent-router", model_sha=args.model_sha) as writer:
            payload["ledger_events_written"] = write_candidate_events(writer, intents)
        _atomic_json(args.output, payload)
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
