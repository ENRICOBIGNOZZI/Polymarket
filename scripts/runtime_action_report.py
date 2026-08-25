#!/usr/bin/env python3
"""Explain what the live paper system did, how it acted, and why it abstained.

The report is diagnostic only. It never changes a trading threshold, manufactures a
fill, or enables authenticated real-money order submission.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def read_rows(path: Path, header_prefix: str | None = None) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    if header_prefix:
        start = next((i for i, line in enumerate(lines) if line.startswith(header_prefix)), None)
        if start is None:
            return []
        lines = lines[start:]
    if not lines:
        return []
    try:
        return list(csv.DictReader(lines))
    except (csv.Error, TypeError):
        return []


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def fnum(value: str | None, default: float = 0.0) -> float:
    try:
        out = float(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def inum(value: str | None, default: int = 0) -> int:
    try:
        return int(float(value if value not in (None, "") else default))
    except (TypeError, ValueError, OverflowError):
        return default


def unique_bundles(rows: Iterable[dict[str, str]]) -> set[str]:
    return {row.get("bundle_id", "") for row in rows if row.get("bundle_id")}


def max_value(rows: Iterable[dict[str, str]], key: str) -> float:
    return max((fnum(row.get(key), float("-inf")) for row in rows), default=0.0)


def positive_count(rows: Iterable[dict[str, str]], key: str, threshold: float = 0.0) -> int:
    return sum(fnum(row.get(key), float("-inf")) > threshold for row in rows)


def recent_rows(
    rows: Iterable[dict[str, str]],
    now: int,
    window: int,
    keys: tuple[str, ...],
) -> list[dict[str, str]]:
    cutoff = now - max(1, window)
    out: list[dict[str, str]] = []
    for row in rows:
        ts = max((inum(row.get(key)) for key in keys), default=0)
        if ts >= cutoff:
            out.append(row)
    return out


def top_row(rows: list[dict[str, str]], key: str) -> dict[str, str] | None:
    if not rows:
        return None
    return max(rows, key=lambda row: fnum(row.get(key), float("-inf")))


def parse_external_timestamp(value: str | None) -> int:
    if value in (None, ""):
        return 0
    numeric = inum(value)
    if numeric > 0:
        return numeric
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except (TypeError, ValueError, OverflowError):
        return 0


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def build_report(
    run_root: Path,
    external_path: Path,
    now: int,
    window: int,
    production_edge: float,
) -> dict[str, Any]:
    b1 = read_rows(run_root / "stat_arb_pairs.csv")
    b2_coherent = read_rows(run_root / "stat_arb_pca.csv")
    raw_path = run_root / "stat_arb_pca_raw.csv"
    b2_raw = read_rows(raw_path) if raw_path.exists() else list(b2_coherent)
    b2_rejected = read_rows(run_root / "stat_arb_pca_rejected.csv")
    structural = read_rows(run_root / "structural_latest.csv", "type,event_id,")
    rewards = read_rows(run_root / "reward_opportunities.csv")
    intents = read_rows(run_root / "intents.csv")
    bundles = read_rows(run_root / "multileg_bundles.csv")
    legs = read_rows(run_root / "multileg_legs.csv")
    events = read_rows(run_root / "multileg_events.csv")
    ledger = read_rows(run_root / "bundle_ledger.csv")
    supervisor = read_rows(run_root / "runtime_supervisor.csv")
    external = read_rows(external_path)
    graph_research = read_json(run_root / "graph_research_status.json")
    graph_research_rows = read_rows(run_root / "graph_research_ev.csv")
    micro_taker = read_json(run_root / "micro_taker" / "status.json")
    micro_exploration = (
        micro_taker.get("exploration")
        if isinstance(micro_taker.get("exploration"), dict)
        else {}
    )

    recent_events = recent_rows(events, now, window, ("timestamp",))
    recent_ledger = recent_rows(ledger, now, window, ("closed_ts", "timestamp"))
    fresh_external = [
        row
        for row in external
        if 0 < parse_external_timestamp(row.get("timestamp"))
        and now - parse_external_timestamp(row.get("timestamp")) <= window
        and fnum(row.get("confidence")) > 0.0
    ]

    b1_raw_positive = positive_count(b1, "raw_expected_edge")
    b1_maker = positive_count(b1, "maker_entry_net_edge", production_edge)
    b1_taker = positive_count(b1, "taker_net_edge", production_edge)
    b2_raw_positive = positive_count(b2_raw, "raw_expected_edge")
    b2_coherent_raw_positive = positive_count(b2_coherent, "raw_expected_edge")
    b2_maker = positive_count(b2_coherent, "maker_entry_net_edge", production_edge)
    b2_taker = positive_count(b2_coherent, "taker_net_edge", production_edge)
    rejected_positive = positive_count(b2_rejected, "raw_expected_edge")

    structural_raw = positive_count(structural, "raw_edge")
    structural_net = sum(
        fnum(row.get("net_edge_pre_gas")) > 0.0
        and fnum(row.get("executable_shares")) > 0.0
        for row in structural
    )
    structural_clob = sum(
        row.get("type") == "BUY_ALL_YES"
        and fnum(row.get("net_edge_pre_gas")) > 0.0
        and fnum(row.get("executable_shares")) > 0.0
        for row in structural
    )
    reward_positive = positive_count(rewards, "conservative_daily_score")

    intent_bundles = unique_bundles(intents)
    known_bundles = {row.get("bundle_id", "") for row in bundles if row.get("bundle_id")}
    not_observed_by_broker = sorted(intent_bundles - known_bundles)
    bundle_status = Counter(row.get("status", "UNKNOWN") or "UNKNOWN" for row in bundles)
    event_counts = Counter(row.get("event", "UNKNOWN") or "UNKNOWN" for row in recent_events)
    abort_reasons = Counter(
        row.get("abort_reason", "") or "unspecified"
        for row in recent_ledger
        if (row.get("status") or "").upper() == "UNWOUND"
    )

    latest_supervisor = supervisor[-1] if supervisor else {}
    recorder_alive = inum(latest_supervisor.get("recorder_alive")) == 1
    broker_alive = inum(latest_supervisor.get("broker_alive")) == 1

    reasons: list[dict[str, str]] = []
    actions: list[str] = []

    if not recorder_alive or not broker_alive:
        reasons.append(
            {
                "code": "RUNTIME_DEGRADED",
                "detail": (
                    f"recorder_alive={int(recorder_alive)} "
                    f"broker_alive={int(broker_alive)}"
                ),
            }
        )
    if b2_rejected:
        reasons.append(
            {
                "code": "INCOHERENT_HEDGE_FILTER",
                "detail": (
                    f"rejected {len(b2_rejected)} PCA baskets because one or more "
                    "hedge legs were outside the target event/semantic cluster"
                ),
            }
        )
    if rejected_positive and b2_coherent_raw_positive == 0:
        reasons.append(
            {
                "code": "SPURIOUS_CROSS_DOMAIN_ALPHA",
                "detail": (
                    f"{rejected_positive} raw-positive PCA candidates existed only "
                    "through economically unrelated hedge legs"
                ),
            }
        )
    executable_raw_positive = b1_raw_positive + b2_coherent_raw_positive
    if executable_raw_positive > 0 and b1_maker + b2_maker == 0:
        reasons.append(
            {
                "code": "COST_BLOCKED",
                "detail": (
                    "economically coherent statistical dislocations exist, but none "
                    "clear the executable maker edge threshold after costs"
                ),
            }
        )
    if b1_maker + b2_maker == 0:
        reasons.append(
            {
                "code": "NO_EXECUTABLE_STAT_ARB",
                "detail": "B1/B2 produced no production-admissible hedge bundle",
            }
        )
    if structural_net > 0 and structural_clob == 0:
        reasons.append(
            {
                "code": "CONVERSION_NOT_PROMOTED",
                "detail": (
                    "positive NegRisk diagnostics require conversion gas/latency and "
                    "remain non-executable"
                ),
            }
        )
    if structural_clob == 0:
        reasons.append(
            {
                "code": "NO_CLOB_STRUCTURAL_ARB",
                "detail": "no positive complete BUY_ALL_YES basket at executable size",
            }
        )
    if graph_research:
        reasons.append(
            {
                "code": "GRAPH_RESEARCH_ONLY",
                "detail": (
                    f"{int(graph_research.get('candidate_bundles') or 0)} Graph baskets are "
                    "scored with empirical joint-fill EV and are not broker-routed"
                ),
            }
        )
    if micro_exploration.get("enabled") is True and int(micro_exploration.get("candidate_strata_last_tick") or 0) == 0:
        reasons.append(
            {
                "code": "EXPLORATION_WAITING_FOR_ACTIVITY",
                "detail": "the bounded taker probe has no eligible recent public trade/activity stratum yet",
            }
        )
    if reward_positive == 0 and rewards:
        reasons.append(
            {
                "code": "REWARDS_NOT_ECONOMIC",
                "detail": (
                    "no standalone liquidity-reward candidate remains positive after "
                    "payout floor, capital, and adverse-selection budgets"
                ),
            }
        )
    if not fresh_external:
        reasons.append(
            {
                "code": "NO_FRESH_EXTERNAL_SIGNAL",
                "detail": (
                    "external expert abstains because the configured feed has no fresh "
                    "positive-confidence rows"
                ),
            }
        )
    if intent_bundles and not_observed_by_broker:
        reasons.append(
            {
                "code": "BROKER_ADMISSION_GAP",
                "detail": (
                    f"{len(not_observed_by_broker)} intent bundles are not present in "
                    "broker state"
                ),
            }
        )

    if b2_rejected:
        actions.append(
            f"FILTER: blocked {len(b2_rejected)} economically incoherent B2 baskets"
        )
    if event_counts:
        actions.append(
            "broker events in the window: "
            + ", ".join(f"{key}={value}" for key, value in sorted(event_counts.items()))
        )
    if bundle_status:
        actions.append(
            "bundle state: "
            + ", ".join(f"{key}={value}" for key, value in sorted(bundle_status.items()))
        )
    if graph_research:
        actions.append(
            "RESEARCH: Graph joint-fill EV: "
            f"{int(graph_research.get('candidate_bundles') or 0)} candidates, "
            f"{int(graph_research.get('economic_research_candidates') or 0)} economic research candidates, "
            "0 broker intents"
        )
    if micro_exploration.get("enabled") is True:
        actions.append(
            "PAPER EXPLORE: micro taker "
            f"active={int(micro_exploration.get('active_positions') or 0)}, "
            f"opens_hour={int(micro_exploration.get('hourly_opens') or 0)}, "
            f"realized_pnl={fnum(str(micro_exploration.get('realized_pnl_total') or 0.0)):.6f} USD"
        )
    if not intent_bundles:
        actions.append("ABSTAIN: no bundle passed coherence, costs, and risk gates")
    elif event_counts.get("POST", 0) == 0 and not known_bundles:
        actions.append("BLOCKED: intents exist but the broker has not admitted them")
    elif event_counts.get("PARTIAL_FILL", 0) == 0 and (
        bundle_status.get("RESTING", 0) or bundle_status.get("COMPLETE", 0)
    ):
        actions.append("WAIT: admitted maker hedge is behind queue and has no evidenced fill")
    if event_counts.get("PARTIAL_FILL", 0):
        actions.append(f"ACT: processed {event_counts['PARTIAL_FILL']} partial-fill events")
    if recent_ledger:
        net = sum(fnum(row.get("net_pnl")) for row in recent_ledger)
        actions.append(
            f"CLOSE/UNWIND: {len(recent_ledger)} baskets, net_pnl={net:.6f} USD"
        )

    return {
        "schema": "polymarket_runtime_action_report_v1",
        "generated_ts": now,
        "generated_utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "window_seconds": window,
        "production_edge_threshold": production_edge,
        "runtime": {
            "recorder_alive": recorder_alive,
            "broker_alive": broker_alive,
            "supervisor_timestamp": inum(latest_supervisor.get("timestamp")),
        },
        "candidate_funnel": {
            "B1_pairs": {
                "rows": len(b1),
                "raw_positive": b1_raw_positive,
                "taker_positive": b1_taker,
                "maker_admissible": b1_maker,
                "best_maker_edge": max_value(b1, "maker_entry_net_edge"),
            },
            "B2_pca_hedges": {
                "rows": len(b2_coherent),
                "raw_rows": len(b2_raw),
                "coherent_rows": len(b2_coherent),
                "rejected_incoherent": len(b2_rejected),
                "raw_positive": b2_raw_positive,
                "coherent_raw_positive": b2_coherent_raw_positive,
                "rejected_raw_positive": rejected_positive,
                "taker_positive": b2_taker,
                "maker_admissible": b2_maker,
                "best_raw_maker_edge": max_value(b2_raw, "maker_entry_net_edge"),
                "best_maker_edge": max_value(b2_coherent, "maker_entry_net_edge"),
            },
            "NegRisk": {
                "rows": len(structural),
                "raw_positive": structural_raw,
                "net_positive_pre_gas": structural_net,
                "clob_complete_set_positive": structural_clob,
            },
            "Graph_research": {
                "rows": len(graph_research_rows),
                "candidate_bundles": int(graph_research.get("candidate_bundles") or 0),
                "economic_research_candidates": int(graph_research.get("economic_research_candidates") or 0),
                "insufficient_evidence_candidates": int(graph_research.get("insufficient_evidence_candidates") or 0),
                "broker_routing_enabled": bool(graph_research.get("broker_routing_enabled", False)),
                "raw_scanner_edge_is_execution_edge": bool(graph_research.get("raw_scanner_edge_is_execution_edge", False)),
            },
            "micro_taker_exploration": {
                "enabled": micro_exploration.get("enabled") is True,
                "active_positions": int(micro_exploration.get("active_positions") or 0),
                "hourly_opens": int(micro_exploration.get("hourly_opens") or 0),
                "candidate_strata_last_tick": int(micro_exploration.get("candidate_strata_last_tick") or 0),
                "realized_pnl_total": fnum(str(micro_exploration.get("realized_pnl_total") or 0.0)),
                "hold_seconds": int(micro_exploration.get("hold_seconds") or 0),
            },
            "B3_rewards_shadow": {
                "rows": len(rewards),
                "standalone_positive": reward_positive,
                "best_daily_score": max_value(rewards, "conservative_daily_score"),
            },
            "external": {"rows": len(external), "fresh_rows": len(fresh_external)},
            "intents": {
                "rows": len(intents),
                "bundles": len(intent_bundles),
                "strategies": dict(
                    Counter(row.get("strategy", "UNKNOWN") for row in intents)
                ),
            },
        },
        "best_candidates": {
            "B1": top_row(b1, "maker_entry_net_edge"),
            "B2_raw": top_row(b2_raw, "maker_entry_net_edge"),
            "B2": top_row(b2_coherent, "maker_entry_net_edge"),
            "B2_rejected": top_row(b2_rejected, "maker_entry_net_edge"),
            "NegRisk": top_row(structural, "net_edge_pre_gas"),
            "B3_rewards": top_row(rewards, "conservative_daily_score"),
        },
        "broker": {
            "bundle_status": dict(sorted(bundle_status.items())),
            "intent_bundles_not_in_state": not_observed_by_broker[:20],
            "recent_event_counts": dict(sorted(event_counts.items())),
            "recent_abort_reasons": dict(sorted(abort_reasons.items())),
            "recent_ledger_rows": len(recent_ledger),
            "recent_net_pnl_usd": sum(
                fnum(row.get("net_pnl")) for row in recent_ledger
            ),
            "live_legs": sum(
                (row.get("exited") or "0") not in {"1", "true", "True"}
                for row in legs
            ),
        },
        "actions": actions,
        "reasons": reasons,
    }


def compact_candidate(row: dict[str, str] | None, fields: tuple[str, ...]) -> str:
    if not row:
        return "none"
    parts = [
        f"{field}={row.get(field, '')}"
        for field in fields
        if row.get(field, "") != ""
    ]
    return " ".join(parts) if parts else "present"


def render_markdown(report: dict[str, Any]) -> str:
    funnel = report["candidate_funnel"]
    b2 = funnel["B2_pca_hedges"]
    graph_research = funnel["Graph_research"]
    exploration = funnel["micro_taker_exploration"]
    broker = report["broker"]
    best = report["best_candidates"]
    runtime = report["runtime"]
    lines = [
        "# Polymarket hourly action report",
        "",
        (
            f"Generated: `{report['generated_utc']}` · window: "
            f"`{report['window_seconds']}s` · production edge gate: "
            f"`{report['production_edge_threshold']:.6f}`"
        ),
        "",
        "## What the system did",
        "",
        (
            f"- Runtime: recorder={'alive' if runtime['recorder_alive'] else 'DOWN'}, "
            f"broker={'alive' if runtime['broker_alive'] else 'DOWN'}."
        ),
        (
            f"- B1 pairs: {funnel['B1_pairs']['rows']} candidates; raw-positive "
            f"{funnel['B1_pairs']['raw_positive']}; maker-admissible "
            f"{funnel['B1_pairs']['maker_admissible']}; best maker edge "
            f"{funnel['B1_pairs']['best_maker_edge']:.6f}."
        ),
        (
            f"- B2 PCA: raw {b2['raw_rows']}; coherent {b2['coherent_rows']}; "
            f"rejected {b2['rejected_incoherent']}; raw-positive "
            f"{b2['raw_positive']}; coherent raw-positive "
            f"{b2['coherent_raw_positive']}; maker-admissible "
            f"{b2['maker_admissible']}; best coherent maker edge "
            f"{b2['best_maker_edge']:.6f}."
        ),
        (
            f"- NegRisk: {funnel['NegRisk']['rows']} diagnostics; net-positive "
            f"pre-gas {funnel['NegRisk']['net_positive_pre_gas']}; CLOB-only "
            f"complete baskets positive {funnel['NegRisk']['clob_complete_set_positive']}."
        ),
        (
            f"- Graph research: {graph_research['candidate_bundles']} baskets; "
            f"economic research candidates {graph_research['economic_research_candidates']}; "
            f"broker routing {'ON' if graph_research['broker_routing_enabled'] else 'OFF'}; "
            "raw scanner edge is not execution edge."
        ),
        (
            f"- Micro taker exploration: {'enabled' if exploration['enabled'] else 'off'}; "
            f"active {exploration['active_positions']}; opens/hour {exploration['hourly_opens']}; "
            f"eligible strata {exploration['candidate_strata_last_tick']}; "
            f"realized PnL {exploration['realized_pnl_total']:.6f} USD; "
            f"hold {exploration['hold_seconds']}s."
        ),
        (
            f"- External expert: {funnel['external']['fresh_rows']} fresh signals "
            f"from {funnel['external']['rows']} configured rows."
        ),
        (
            f"- Intent adapter: {funnel['intents']['bundles']} bundles / "
            f"{funnel['intents']['rows']} legs. Broker states: "
            f"{broker['bundle_status'] or {}}."
        ),
        "",
        "## How it acted",
        "",
    ]
    if report["actions"]:
        lines.extend(f"- {action}" for action in report["actions"])
    else:
        lines.append("- No order-state action was recorded in the reporting window.")
    lines += ["", "## Why", ""]
    if report["reasons"]:
        lines.extend(
            f"- `{item['code']}` — {item['detail']}." for item in report["reasons"]
        )
    else:
        lines.append("- No blocking condition detected.")
    lines += [
        "",
        "## Best hedge/dislocation candidates",
        "",
        "- B1: "
        + compact_candidate(
            best["B1"],
            (
                "y_slug",
                "x_slug",
                "z",
                "raw_expected_edge",
                "maker_entry_net_edge",
                "taker_net_edge",
            ),
        ),
        "- B2 raw: "
        + compact_candidate(
            best["B2_raw"],
            (
                "slug",
                "residual_z",
                "hedge_error",
                "raw_expected_edge",
                "maker_entry_net_edge",
                "legs",
            ),
        ),
        "- B2 coherent: "
        + compact_candidate(
            best["B2"],
            (
                "slug",
                "residual_z",
                "hedge_error",
                "raw_expected_edge",
                "maker_entry_net_edge",
                "coherence_scope",
                "legs",
            ),
        ),
        "- B2 rejected: "
        + compact_candidate(
            best["B2_rejected"],
            (
                "slug",
                "raw_expected_edge",
                "maker_entry_net_edge",
                "coherence_reason",
                "unrelated_market_ids",
                "legs",
            ),
        ),
        "- NegRisk: "
        + compact_candidate(
            best["NegRisk"],
            (
                "type",
                "event_id",
                "raw_edge",
                "net_edge_pre_gas",
                "executable_shares",
            ),
        ),
        "- Rewards shadow: "
        + compact_candidate(
            best["B3_rewards"],
            (
                "question",
                "conditional_conservative_daily_score",
                "conservative_daily_score",
                "payout_shortfall_usd",
            ),
        ),
        "",
        "## Broker evidence",
        "",
        f"- Recent event counts: `{broker['recent_event_counts'] or {}}`.",
        (
            f"- Recent closed/unwound baskets: `{broker['recent_ledger_rows']}`; "
            f"net PnL: `{broker['recent_net_pnl_usd']:.6f}` USD."
        ),
        (
            "- Intent bundles absent from broker state: "
            f"`{len(broker['intent_bundles_not_in_state'])}`."
        ),
        "",
        (
            "The report is explanatory only. It does not lower gates, fabricate "
            "trades, or enable authenticated real-money submission."
        ),
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--run-root", type=Path, default=Path("runs/paper_v4_live")
    )
    parser.add_argument(
        "--external-signals", type=Path, default=Path("data/external_signals.csv")
    )
    parser.add_argument("--window-seconds", type=int, default=3600)
    parser.add_argument("--production-edge", type=float, default=0.001)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-markdown", type=Path)
    parser.add_argument("--now", type=int, default=None, help="deterministic test override")
    args = parser.parse_args()
    now = int(time.time()) if args.now is None else args.now
    report = build_report(
        args.run_root,
        args.external_signals,
        now,
        args.window_seconds,
        args.production_edge,
    )
    markdown = render_markdown(report)
    if args.output_json:
        atomic_write(
            args.output_json, json.dumps(report, indent=2, sort_keys=True) + "\n"
        )
    if args.output_markdown:
        atomic_write(args.output_markdown, markdown)
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
