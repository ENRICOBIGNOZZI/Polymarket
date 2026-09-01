#!/usr/bin/env python3
"""Reconstruct and attribute External Fair virtual terminal trades.

This is a read-only economic autopsy.  It does not treat SHADOW fills as real
PnL and it never combines historical or mixed-SHA lifecycles into an exact-SHA
claim.  Missing fields are reported rather than imputed.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import subprocess
import time
from typing import Any, Iterable

try:
    from v7_external_economic_common import (
        atomic_json, canonical_sha256, finite, group_trade_lifecycles,
        lineage_state, load_counterfactual_evidence, nearest_prior_forecast,
    )
except ModuleNotFoundError:
    from scripts.v7_external_economic_common import (
        atomic_json, canonical_sha256, finite, group_trade_lifecycles,
        lineage_state, load_counterfactual_evidence, nearest_prior_forecast,
    )


SCHEMA = "polymarket_v7_external_loss_attribution_v1"
REQUIRED_CAUSAL_FIELDS = (
    "contract_rules_hash", "reference_version", "decision_exchange_ts_ms",
    "decision_receive_ts_ms", "arrival_exchange_ts_ms", "arrival_receive_ts_ms",
    "tte_seconds", "fair_yes", "fair_yes_lower", "fair_yes_upper",
    "decision_pm_mid", "decision_bid", "decision_ask", "arrival_fill_price",
    "filled_size", "entry_fee", "settlement_outcome", "realized_pnl",
)


def _metadata(row: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    value = row.get("metadata")
    return value if isinstance(value, dict) else {}


def _threshold_for_tte(tte: float | None, policy: dict[str, Any]) -> float | None:
    if tte is None:
        return None
    buckets = policy.get("tte_bucket_policy")
    if isinstance(buckets, list):
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            minimum = finite(bucket.get("minimum_seconds"))
            maximum = finite(bucket.get("maximum_seconds"))
            if minimum is not None and maximum is not None and minimum <= tte <= maximum:
                return finite(bucket.get("minimum_robust_ev_per_share"))
    return finite(policy.get("minimum_robust_ev_per_share"))


def _outcome_actual_yes(final: dict[str, Any], outcome: str) -> float | None:
    metadata = _metadata(final)
    won = metadata.get("won")
    if not isinstance(won, bool) or outcome not in {"YES", "NO"}:
        return None
    return float(won == (outcome == "YES"))


def _markout_values(rows: Iterable[dict[str, Any]]) -> dict[str, float]:
    values: dict[str, float] = {}
    for row in rows:
        markouts = row.get("markouts") if isinstance(row.get("markouts"), dict) else {}
        for horizon, raw in markouts.items():
            value = finite(raw)
            if value is not None:
                values[str(horizon)] = value
    return dict(sorted(values.items(), key=lambda item: (
        int(item[0].removesuffix("s")) if item[0].removesuffix("s").isdigit() else 10**9,
        item[0],
    )))


def reconstruct_trade(
    lifecycle: dict[str, Any], rows: list[dict[str, Any]], current_sha: str,
    policy: dict[str, Any], accounting_tolerance: float = 1e-8,
) -> dict[str, Any]:
    candidate = lifecycle.get("candidate") if isinstance(lifecycle.get("candidate"), dict) else {}
    fill = lifecycle.get("fill") if isinstance(lifecycle.get("fill"), dict) else {}
    final = lifecycle.get("final") if isinstance(lifecycle.get("final"), dict) else {}
    candidate_meta, fill_meta, final_meta = _metadata(candidate), _metadata(fill), _metadata(final)
    market_id = str(fill.get("market_id") or final.get("market_id") or candidate.get("market_id") or "")
    entry_ts = int(finite(fill.get("timestamp_ms"), finite(fill.get("receive_ts_ms"), 0.0)) or 0)
    forecast = nearest_prior_forecast(rows, market_id, entry_ts)
    forecast = forecast if isinstance(forecast, dict) else {}

    outcome = str(fill_meta.get("outcome") or candidate_meta.get("outcome") or "").upper()
    shares = finite(fill.get("filled_size"))
    price = finite(fill.get("fill_price"))
    fee = finite(fill.get("fee"), 0.0)
    slippage = finite(fill.get("slippage"), 0.0)
    payout = finite(final.get("virtual_cashflow"))
    realized = finite(final.get("counterfactual_pnl"))
    recomputed = None
    accounting_error = None
    if None not in (shares, price, fee, payout):
        recomputed = float(payout) - float(shares) * float(price) - float(fee)
        if realized is not None:
            accounting_error = realized - recomputed

    fair_yes = finite(fill_meta.get("fair_yes"), finite(candidate_meta.get("fair_yes")))
    fair_lower = finite(fill_meta.get("fair_lower"), finite(candidate_meta.get("fair_lower")))
    fair_upper = finite(fill_meta.get("fair_upper"), finite(candidate_meta.get("fair_upper")))
    robust_probability = finite(
        fill_meta.get("arrival_robust_probability"),
        finite(fill_meta.get("robust_probability"), finite(candidate_meta.get("robust_probability"))),
    )
    market_yes = finite(fill_meta.get("arrival_pm_mid"), finite(candidate_meta.get("pm_mid")))
    robust_ev_per_share = finite(
        fill_meta.get("arrival_robust_ev_per_share"),
        finite(fill_meta.get("robust_ev_per_share"), finite(candidate_meta.get("robust_ev_per_share"))),
    )
    expected_risk = finite(fill_meta.get("expected_execution_risk"), 0.0)
    fee_per_share = fee / shares if fee is not None and shares not in (None, 0.0) else None
    actual_yes = _outcome_actual_yes(final, outcome)
    probability_error = actual_yes - fair_yes if actual_yes is not None and fair_yes is not None else None
    settlement_margin_error = None
    if actual_yes is not None and fair_yes is not None:
        predicted_yes = fair_yes >= 0.5
        settlement_margin_error = float((actual_yes >= 0.5) != predicted_yes)

    decision_ts = int(finite(candidate.get("decision_ts_ms"), finite(candidate.get("timestamp_ms"), 0.0)) or 0)
    arrival_receive = int(finite(fill.get("receive_ts_ms"), 0.0) or 0)
    arrival_exchange = int(finite(fill.get("exchange_ts_ms"), 0.0) or 0)
    decision_receive = int(finite(candidate.get("receive_ts_ms"), 0.0) or 0)
    decision_exchange = int(finite(candidate.get("exchange_ts_ms"), 0.0) or 0)
    threshold = _threshold_for_tte(finite(fill_meta.get("arrival_tte_seconds"), finite(candidate_meta.get("tte_seconds"))), policy)

    fields: dict[str, Any] = {
        "fill_id": str(lifecycle.get("fill_id") or ""),
        "counterfactual_id": str(fill.get("counterfactual_id") or candidate.get("counterfactual_id") or ""),
        "position_id": str(fill.get("position_id") or final.get("position_id") or ""),
        "market_id": market_id,
        "event_id": str(fill.get("event_id") or final.get("event_id") or candidate.get("event_id") or ""),
        "token_id": str(fill.get("token_id") or final.get("token_id") or ""),
        "outcome": outcome or None,
        "contract_rules_hash": fill_meta.get("contract_rules_hash") or candidate_meta.get("contract_rules_hash"),
        "reference_version": fill_meta.get("reference_version") or candidate_meta.get("reference_version"),
        "decision_exchange_ts_ms": decision_exchange or None,
        "decision_receive_ts_ms": decision_receive or None,
        "decision_ts_ms": decision_ts or None,
        "arrival_exchange_ts_ms": arrival_exchange or None,
        "arrival_receive_ts_ms": arrival_receive or None,
        "decision_to_arrival_ms": (
            arrival_receive - decision_ts if arrival_receive and decision_ts and arrival_receive >= decision_ts else None
        ),
        "arrival_book_age_ms": (
            arrival_receive - arrival_exchange
            if arrival_receive and arrival_exchange and arrival_receive >= arrival_exchange else None
        ),
        "tte_seconds": finite(fill_meta.get("arrival_tte_seconds"), finite(candidate_meta.get("tte_seconds"))),
        "tte_bucket_id": fill_meta.get("tte_bucket_id") or candidate_meta.get("tte_bucket_id"),
        "oracle_value_nearest_prior_forecast": finite(forecast.get("oracle_value")),
        "forecast_timestamp_ms": int(finite(forecast.get("timestamp_ms"), 0.0) or 0) or None,
        "fair_yes": fair_yes,
        "fair_yes_lower": fair_lower,
        "fair_yes_upper": fair_upper,
        "robust_probability_for_side": robust_probability,
        "decision_pm_mid": finite(candidate_meta.get("pm_mid")),
        "arrival_pm_mid": market_yes,
        "decision_bid": finite(candidate.get("bid")),
        "decision_ask": finite(candidate.get("ask")),
        "decision_bid_depth": finite(candidate.get("bid_depth")),
        "decision_ask_depth": finite(candidate.get("ask_depth")),
        "arrival_fill_price": price,
        "filled_size": shares,
        "entry_fee": fee,
        "entry_fee_per_share": fee_per_share,
        "slippage": slippage,
        "expected_execution_risk_per_share": expected_risk,
        "predicted_robust_ev_per_share": robust_ev_per_share,
        "predicted_robust_net_ev": finite(fill_meta.get("robust_net_ev")),
        "policy_threshold_per_share": threshold,
        "settlement_outcome": final_meta.get("settlement_outcome"),
        "winning_token_id": final_meta.get("winning_token_id"),
        "won": final_meta.get("won") if isinstance(final_meta.get("won"), bool) else None,
        "actual_yes": actual_yes,
        "virtual_cashflow": payout,
        "realized_pnl": realized,
        "recomputed_pnl": recomputed,
        "accounting_error": accounting_error,
        "probability_error_actual_minus_fair": probability_error,
        "settlement_direction_error": settlement_margin_error,
        "markout_pnl_per_share": _markout_values(lifecycle.get("markouts", [])),
        "lineage": lineage_state(lifecycle, current_sha),
        "policy_hashes": sorted({
            str(row.get("policy_sha256") or "")
            for row in (candidate, fill, final) if row
        } - {""}),
    }
    missing = [name for name in REQUIRED_CAUSAL_FIELDS if fields.get(name) is None]
    for unavailable in (
        "initial_settlement_reference_value", "terminal_oracle_value",
        "composite_spot", "composite_microprice", "venue_dispersion",
        "order_flow_imbalance", "trade_imbalance", "arrival_book_levels",
        "empirical_signing_latency_ms", "empirical_ack_latency_ms",
        "settlement_margin", "settlement_margin_sigma",
    ):
        missing.append(unavailable)
    fields["missing_causal_fields"] = sorted(set(missing))

    causes: list[str] = []
    if accounting_error is not None and abs(accounting_error) > accounting_tolerance:
        causes.append("ACCOUNTING_MISMATCH")
    if realized is not None and realized < 0.0:
        if fields["won"] is False:
            causes.append("SELECTED_SIDE_SETTLED_FALSE")
        if probability_error is not None:
            causes.append("SETTLEMENT_PROBABILITY_ERROR")
        if robust_ev_per_share is not None and threshold is not None and robust_ev_per_share <= threshold + 0.0025:
            causes.append("NEAR_POLICY_THRESHOLD")
        if market_yes is not None and fair_yes is not None:
            side_market = market_yes if outcome == "YES" else 1.0 - market_yes
            side_fair = fair_yes if outcome == "YES" else 1.0 - fair_yes
            if side_fair > side_market:
                causes.append("MODEL_MARKET_DISAGREEMENT_SELECTED")
    if fields["lineage"]["state"] == "MIXED_SHA":
        causes.append("MIXED_SHA_EVIDENCE_LIMITATION")
    if fields["missing_causal_fields"]:
        causes.append("INCOMPLETE_RECORDED_CAUSAL_FEATURES")
    fields["attribution_flags"] = sorted(set(causes))
    fields["accounting_reconciled"] = bool(
        accounting_error is not None and abs(accounting_error) <= accounting_tolerance
    )
    return fields


def _group_summary(trades: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        groups[str(trade.get(field) or "UNKNOWN")].append(trade)
    return [
        {
            field: key,
            "terminal_trades": len(values),
            "wins": sum(trade.get("realized_pnl") is not None and trade["realized_pnl"] > 0 for trade in values),
            "losses": sum(trade.get("realized_pnl") is not None and trade["realized_pnl"] <= 0 for trade in values),
            "realized_pnl": sum(float(trade.get("realized_pnl") or 0.0) for trade in values),
            "predicted_robust_net_ev": sum(float(trade.get("predicted_robust_net_ev") or 0.0) for trade in values),
        }
        for key, values in sorted(groups.items())
    ]


def build_attribution(
    rows: list[dict[str, Any]], quality: dict[str, Any], current_sha: str,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = policy if isinstance(policy, dict) else {}
    lifecycles = group_trade_lifecycles(rows)
    trades = [
        reconstruct_trade(lifecycle, rows, current_sha, policy)
        for _, lifecycle in sorted(lifecycles.items())
    ]
    terminal = [trade for trade in trades if trade.get("realized_pnl") is not None]
    losses = [trade for trade in terminal if float(trade["realized_pnl"]) <= 0.0]
    flags = Counter(flag for trade in losses for flag in trade["attribution_flags"])
    lineage = Counter(trade["lineage"]["state"] for trade in trades)
    exact_terminal = [trade for trade in terminal if trade["lineage"]["state"] == "EXACT_SHA"]
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at_unix_ms": int(time.time() * 1000),
        "repository_head": current_sha,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "economic_authority": "SHADOW_COUNTERFACTUAL",
        "profitability_proven": False,
        "source_quality": quality,
        "summary": {
            "lifecycles": len(trades),
            "terminal_trades": len(terminal),
            "open_trades": len(trades) - len(terminal),
            "wins": len(terminal) - len(losses),
            "losses": len(losses),
            "realized_pnl": sum(float(trade["realized_pnl"]) for trade in terminal),
            "predicted_robust_net_ev": sum(float(trade.get("predicted_robust_net_ev") or 0.0) for trade in terminal),
            "exact_sha_terminal_trades": len(exact_terminal),
            "exact_sha_realized_pnl": sum(float(trade["realized_pnl"]) for trade in exact_terminal),
            "lineage_states": dict(sorted(lineage.items())),
            "loss_attribution_flags": dict(sorted(flags.items())),
            "all_terminal_accounting_reconciled": bool(terminal) and all(
                trade["accounting_reconciled"] for trade in terminal
            ),
        },
        "by_side": _group_summary(terminal, "outcome"),
        "by_tte_bucket": _group_summary(terminal, "tte_bucket_id"),
        "by_lineage_state": _group_summary(terminal, "lineage_state"),
        "trades": trades,
        "interpretation": {
            "current_head_profitability_claim": "NOT_PROVEN",
            "historical_rows_are_current_state": False,
            "mixed_sha_lifecycles_are_exact_sha_evidence": False,
            "missing_fields_are_imputed": False,
            "observed_virtual_pnl_is_real_pnl": False,
        },
    }
    # _group_summary accepts top-level fields; materialize this derived value
    # only for the summary and remove it from trade rows afterwards.
    for trade in terminal:
        trade["lineage_state"] = trade["lineage"]["state"]
    payload["by_lineage_state"] = _group_summary(terminal, "lineage_state")
    for trade in terminal:
        trade.pop("lineage_state", None)
    payload["content_sha256"] = canonical_sha256(payload)
    return payload


def _load_policy(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    taker = value.get("taker")
    return taker if isinstance(taker, dict) else value


def current_head(repo: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True,
    ).strip()


def generate(
    inputs: Iterable[Path], repo: Path, output: Path,
    config: Path | None = None,
) -> dict[str, Any]:
    rows, quality = load_counterfactual_evidence(inputs)
    report = build_attribution(rows, quality, current_head(repo), _load_policy(config))
    atomic_json(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--config", type=Path, default=Path("config/v7_external_fair.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = generate(args.input, args.repo.resolve(), args.output, args.config)
    print(json.dumps(report["summary"], indent=2, sort_keys=True))
    return 2 if report["source_quality"]["fail_closed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
