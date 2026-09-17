#!/usr/bin/env python3
"""Slow-path crypto-only selector for V7 professional market making.

It ranks the canonical crypto universe from causal public trades, causal books,
fillability evidence and settlement context. It has no market-discovery or
execution authority and never widens the configured crypto universe.
"""
from __future__ import annotations
try:
    from v7_external_rich_model import is_paper_learning_fair
except ModuleNotFoundError:
    # Standalone file-based test loaders need the canonical sibling directory.
    import sys
    from pathlib import Path as _ModulePath
    sys.path.insert(0, str(_ModulePath(__file__).resolve().parent))
    from v7_external_rich_model import is_paper_learning_fair


import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import time
import urllib.parse
import urllib.request
from typing import Any, Callable


SELECTOR_STATUS_SCHEMA = "polymarket_v7_maker_selector_status_v1"
UNIVERSE_SCHEMA = "polymarket_v7_crypto_universe_snapshot_v1"
EXECUTION_AUTHORITY_SEMANTICS = "token_action_side_v2"
EXECUTION_MODEL_SCHEMA = "polymarket_v7_maker_execution_model_v1"
EXECUTION_SEMANTICS = "maker-paper-v7.2-bilateral-inventory"
FLOW_EXECUTION_AUTHORITY_BASES = frozenset({
    "FRESH_OPPOSITE_FLOW",
    "POSITIVE_FLOW_CONTROL",
    "LOW_SAMPLE_FRESH_FLOW_CONTROL",
})
ANCHOR_FLOW_FIELDS = (
    "recent_prints", "recent_unique_transactions", "recent_share_volume",
    "recent_notional_usd", "recent_flow_to_liquidity", "recent_last_trade_age_ms",
    "recent_buy_prints_5s", "recent_buy_prints_30s", "recent_buy_prints_2m",
    "recent_buy_prints_10m", "recent_buy_share_volume_10m",
    "recent_buy_notional_usd_10m", "recent_last_buy_age_ms",
    "recent_sell_prints_5s", "recent_sell_prints_30s", "recent_sell_prints_2m",
    "recent_sell_prints_10m", "recent_sell_share_volume_10m",
    "recent_sell_notional_usd_10m", "recent_last_sell_age_ms",
)


def _preserved_anchor_flow(
    row: dict[str, Any] | None, *, yes_token: str, no_token: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Preserve only receive-time-causal flow authority already earned upstream."""
    if not isinstance(row, dict):
        return [], [], {}
    valid_tokens = {yes_token, no_token}
    quotes = [
        dict(q) for q in row.get("quote_opportunities", [])
        if isinstance(q, dict)
        and str(q.get("token_id") or "") in valid_tokens
        and q.get("opposite_flow_is_fresh") is True
    ]
    quote_ids = {
        (str(q.get("token_id") or ""), str(q.get("outcome") or "").upper(),
         str(q.get("quote_side") or "").upper())
        for q in quotes
    }
    cells = [
        dict(cell) for cell in row.get("authorized_execution_cells", [])
        if isinstance(cell, dict)
        and str(cell.get("authority_basis") or "") in FLOW_EXECUTION_AUTHORITY_BASES
        and str(cell.get("token_id") or "") in valid_tokens
        and (str(cell.get("token_id") or ""),
             str(cell.get("outcome") or "").upper(),
             str(cell.get("quote_side") or "").upper()) in quote_ids
    ]
    if not cells:
        return [], [], {}
    fields = {key: row[key] for key in ANCHOR_FLOW_FIELDS if key in row}
    return cells, quotes, fields


def _exact_cell_identity(
    market_id: Any, token_id: Any, action: Any, quote_side: Any,
) -> tuple[str, str, str, str]:
    return (
        str(market_id or ""), str(token_id or ""),
        str(action or "").upper(), str(quote_side or "").upper(),
    )


def _load_exact_cell_evidence(
    path: Path | None, *, model_sha: str,
) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    """Load optional exact-policy selector feedback from the current research execution model.

    Invalid or stale feedback cannot grant authority and is therefore ignored.
    The C++ runtime remains the only component allowed to admit a quote.
    """
    if path is None or not path.is_file():
        return {}
    try:
        model = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    exact = model.get("exact_execution_cells")
    cells = exact.get("cells") if isinstance(exact, dict) else None
    if (
        model.get("schema") != EXECUTION_MODEL_SCHEMA
        or model.get("paper_only") is not True
        or model.get("authenticated_execution") is not False
        or model.get("real_order_submission") is not False
        or model.get("model_sha") != model_sha
        or model.get("execution_semantics_version") != EXECUTION_SEMANTICS
        or not isinstance(exact, dict)
        or exact.get("identity") != [
            "market_id", "token_id", "action", "quote_side"]
        or exact.get("semantics")
            != "exact_cell_terminal_funnel_current_policy_only_v1"
        or not isinstance(cells, list)
    ):
        return {}
    output: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for raw in cells:
        if not isinstance(raw, dict):
            continue
        key = _exact_cell_identity(
            raw.get("market_id"), raw.get("token_id"),
            raw.get("action"), raw.get("quote_side"),
        )
        if (
            not key[0] or not key[1]
            or key[2] not in {"JOIN", "IMPROVE1", "ONE_SIDED", "FADE1", "FADE2"}
            or key[3] not in {"BUY", "SELL"}
        ):
            continue
        output[key] = raw
    return output


def _annotate_exact_cell_evidence(
    row: dict[str, Any], cell: dict[str, Any],
    evidence: dict[tuple[str, str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    result = dict(cell)
    raw = evidence.get(_exact_cell_identity(
        row.get("market_id"), cell.get("token_id"),
        cell.get("action"), cell.get("quote_side"),
    ), {})
    result["durable_exact_cell_evidence"] = {
        "terminal_orders": max(0, int(finite(raw.get("terminal_orders"), 0.0))),
        "no_opposite_flow": max(0, int(finite(raw.get("no_opposite_flow"), 0.0))),
        "price_not_reached": max(0, int(finite(raw.get("price_not_reached"), 0.0))),
        "queue_not_depleted": max(0, int(finite(raw.get("queue_not_depleted"), 0.0))),
        "filled_orders": max(0, int(finite(raw.get("filled_orders"), 0.0))),
        "last_terminal_ts_ms": max(
            0, int(finite(raw.get("last_terminal_ts_ms"), 0.0))),
        "role": "RANKING_ONLY_NO_EXECUTION_OR_RISK_AUTHORITY",
    }
    return result


def _control_exploration_cell(row: dict[str, Any]) -> dict[str, Any]:
    """Return the best observed sub-threshold flow cell or a cold-start cell.

    The C++ runtime still requires positive point EV before it can quote this
    exact token/action/side.  Here we only stop cold-start exploration from
    choosing the opposite side of the sole fresh aggressor flow.
    """
    opportunities = (
        row.get("quote_opportunities")
        if isinstance(row.get("quote_opportunities"), list) else [])
    best: tuple[float, str, dict[str, Any]] | None = None
    for opportunity in opportunities:
        if not isinstance(opportunity, dict):
            continue
        token_id = str(opportunity.get("token_id") or "")
        outcome = str(opportunity.get("outcome") or "").upper()
        quote_side = str(opportunity.get("quote_side") or "").upper()
        join_probability = finite(
            opportunity.get("projected_join_fill_probability"), 0.0)
        improve_probability = finite(
            opportunity.get("projected_improve1_fill_probability"), 0.0)
        improve_available = opportunity.get("improve1_available") is True
        if improve_available and improve_probability > join_probability:
            action = "IMPROVE1"
            fill_probability = improve_probability
            queue_probability = 1.0
        else:
            action = "JOIN"
            fill_probability = join_probability
            queue_probability = finite(
                opportunity.get("projected_join_queue_depletion_probability"),
                0.0,
            )
        if (
            not token_id or outcome not in {"YES", "NO"}
            or quote_side not in {"BUY", "SELL"}
            or opportunity.get("book_evidence_valid") is not True
            or opportunity.get("opposite_flow_is_fresh") is not True
            or fill_probability <= 0.0
        ):
            continue
        cell = {
            "outcome": outcome,
            "token_id": token_id,
            "action": action,
            "quote_side": quote_side,
            "authority_basis": "POSITIVE_FLOW_CONTROL",
            "projected_flow_reach_probability": max(0.0, finite(
                opportunity.get("projected_flow_reach_probability"), 0.0)),
            "projected_queue_depletion_probability": max(
                0.0, queue_probability),
            "projected_fill_probability": max(0.0, fill_probability),
        }
        identity = "|".join((token_id, action, quote_side))
        candidate = (fill_probability, identity, cell)
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    if best is not None:
        return best[2]
    return {
        "outcome": "YES",
        "token_id": str(row.get("yes_token") or ""),
        "action": "JOIN",
        "quote_side": "BUY",
        "authority_basis": "COLD_START_CONTROL",
        "projected_flow_reach_probability": 0.0,
        "projected_queue_depletion_probability": 0.0,
        "projected_fill_probability": 0.0,
    }


def _control_exploration_cells(
    row: dict[str, Any],
    *,
    minimum_prints_30s: int,
    maximum_last_side_age_ms: int,
) -> list[dict[str, Any]]:
    """Return positive JOIN/IMPROVE arms for one causal market-side cell.

    The main selector deliberately needs more than one print before granting
    ordinary execution authority. A bounded PAPER control lane must be able to
    learn from the first fresh print, otherwise the stricter gate is an
    absorbing state. This helper never infers a side: the public aggressor
    print, token and valid local book must all identify it explicitly.
    """
    opportunities = (
        row.get("quote_opportunities")
        if isinstance(row.get("quote_opportunities"), list) else [])
    best: tuple[float, str, dict[str, Any], str] | None = None
    for opportunity in opportunities:
        if not isinstance(opportunity, dict):
            continue
        token_id = str(opportunity.get("token_id") or "")
        outcome = str(opportunity.get("outcome") or "").upper()
        quote_side = str(opportunity.get("quote_side") or "").upper()
        age_ms = int(finite(opportunity.get("last_opposite_flow_age_ms"), -1.0))
        low_sample_fresh = (
            int(finite(opportunity.get("opposite_prints_30s"), 0.0))
                >= max(1, minimum_prints_30s)
            and 0 <= age_ms <= max(1_000, maximum_last_side_age_ms)
        )
        ordinary_fresh = opportunity.get("opposite_flow_is_fresh") is True
        join_probability = max(0.0, finite(
            opportunity.get("projected_join_fill_probability"), 0.0))
        improve_probability = max(0.0, finite(
            opportunity.get("projected_improve1_fill_probability"), 0.0))
        best_probability = max(
            join_probability,
            improve_probability
                if opportunity.get("improve1_available") is True else 0.0,
        )
        if (
            not token_id or outcome not in {"YES", "NO"}
            or quote_side not in {"BUY", "SELL"}
            or opportunity.get("book_evidence_valid") is not True
            or not (ordinary_fresh or low_sample_fresh)
            or best_probability <= 0.0
        ):
            continue
        authority_basis = (
            "POSITIVE_FLOW_CONTROL" if ordinary_fresh
            else "LOW_SAMPLE_FRESH_FLOW_CONTROL")
        identity = "|".join((token_id, quote_side))
        candidate = (best_probability, identity, opportunity, authority_basis)
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    if best is None:
        return []

    _probability, _identity, opportunity, authority_basis = best
    common = {
        "outcome": str(opportunity["outcome"]).upper(),
        "token_id": str(opportunity["token_id"]),
        "quote_side": str(opportunity["quote_side"]).upper(),
        "authority_basis": authority_basis,
        "projected_flow_reach_probability": max(0.0, finite(
            opportunity.get("projected_flow_reach_probability"), 0.0)),
    }
    cells: list[dict[str, Any]] = []
    join_probability = max(0.0, finite(
        opportunity.get("projected_join_fill_probability"), 0.0))
    if join_probability > 0.0:
        cells.append({
            **common,
            "action": "JOIN",
            "projected_queue_depletion_probability": max(0.0, finite(
                opportunity.get("projected_join_queue_depletion_probability"),
                0.0,
            )),
            "projected_fill_probability": join_probability,
        })
    improve_probability = max(0.0, finite(
        opportunity.get("projected_improve1_fill_probability"), 0.0))
    if opportunity.get("improve1_available") is True and improve_probability > 0.0:
        cells.append({
            **common,
            "action": "IMPROVE1",
            "projected_queue_depletion_probability": 1.0,
            "projected_fill_probability": improve_probability,
        })
    return cells


def _authorize_control_cells(
    rows: list[dict[str, Any]],
    *,
    maximum_markets: int,
    minimum_prints_30s: int,
    maximum_last_side_age_ms: int,
    allow_zero_flow_fallback: bool = True,
    exact_cell_evidence: dict[
        tuple[str, str, str, str], dict[str, Any]] | None = None,
) -> int:
    """Authorize bounded, distinct causal controls without global collapse."""
    exact_cell_evidence = exact_cell_evidence or {}
    maximum_markets = max(1, int(maximum_markets))
    authorized_rows = [
        row for row in rows if row.get("authorized_execution_cells")]
    already_authorized = len(authorized_rows)
    authorized_cells = 0

    # A market may clear the main threshold for IMPROVE1 while its JOIN arm is
    # positive but sub-threshold (or vice versa). Fill-policy learning requires
    # both admissible actions; add the missing arm inside the same market budget
    # instead of treating any one authorized action as a completed experiment.
    for row in authorized_rows[:maximum_markets]:
        controls = _control_exploration_cells(
            row,
            minimum_prints_30s=minimum_prints_30s,
            maximum_last_side_age_ms=maximum_last_side_age_ms,
        )
        existing = {
            (str(cell.get("token_id") or ""), str(cell.get("action") or ""),
             str(cell.get("quote_side") or ""))
            for cell in row.get("authorized_execution_cells", [])
            if isinstance(cell, dict)
        }
        additions = [
            _annotate_exact_cell_evidence(row, cell, exact_cell_evidence)
            for cell in controls
            if (str(cell["token_id"]), str(cell["action"]),
                str(cell["quote_side"])) not in existing
        ]
        if additions:
            row["authorized_execution_cells"].extend(additions)
            row["authorized_execution_cell_count"] = len(
                row["authorized_execution_cells"])
            row["control_exploration_authorized"] = True
            authorized_cells += len(additions)

    remaining_markets = max(0, maximum_markets - already_authorized)
    if remaining_markets <= 0:
        return authorized_cells

    candidates: list[tuple[float, str, dict[str, Any], list[dict[str, Any]]]] = []
    for row in rows:
        if row.get("authorized_execution_cells"):
            continue
        cells = _control_exploration_cells(
            row,
            minimum_prints_30s=minimum_prints_30s,
            maximum_last_side_age_ms=maximum_last_side_age_ms,
        )
        if not cells:
            continue
        probability = max(
            finite(cell.get("projected_fill_probability"), 0.0)
            for cell in cells)
        identity = str(row.get("market_id") or "")
        candidates.append((probability, identity, row, cells))

    for _probability, _identity, row, cells in sorted(
        candidates, key=lambda item: (-item[0], item[1])
    )[:remaining_markets]:
        row["control_exploration_authorized"] = True
        row["execution_role"] = str(cells[0]["authority_basis"])
        row["authorized_execution_cells"] = [
            _annotate_exact_cell_evidence(row, cell, exact_cell_evidence)
            for cell in cells
        ]
        row["authorized_execution_cell_count"] = len(cells)
        authorized_cells += len(cells)

    # A deterministic zero-flow control is historical bootstrap behavior only.
    # Flow-first policies keep the warm universe observable but grant no execution
    # authority when causal opposite-side flow is absent.
    if (
        allow_zero_flow_fallback
        and authorized_cells == 0
        and already_authorized == 0
    ):
        return _authorize_one_control_cell(
            rows, exact_cell_evidence=exact_cell_evidence)
    return authorized_cells


def _authorize_one_control_cell(
    rows: list[dict[str, Any]], *,
    exact_cell_evidence: dict[
        tuple[str, str, str, str], dict[str, Any]] | None = None,
) -> int:
    """Authorize exactly one best research cell when no full flow cell exists."""
    exact_cell_evidence = exact_cell_evidence or {}
    if any(row.get("authorized_execution_cells") for row in rows):
        return 0
    candidates: list[tuple[float, str, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for row in rows:
        cell = _control_exploration_cell(row)
        if not cell["token_id"]:
            continue
        probability = finite(cell.get("projected_fill_probability"), 0.0)
        identity = "|".join((
            str(row.get("market_id") or ""), str(cell["token_id"]),
            str(cell["action"]), str(cell["quote_side"]),
        ))
        history = exact_cell_evidence.get(_exact_cell_identity(
            row.get("market_id"), cell.get("token_id"),
            cell.get("action"), cell.get("quote_side"),
        ), {})
        candidates.append((probability, identity, row, cell, history))
    if not candidates:
        return 0
    # A fresh causal probability always dominates historical quiet-window
    # evidence. With no causal flow, cover untried exact cells first and then
    # revisit the least-observed/oldest cell. This preserves one active control
    # while preventing a single market from consuming every cold-start trial.
    def rank(item: tuple[
        float, str, dict[str, Any], dict[str, Any], dict[str, Any]
    ]) -> tuple[Any, ...]:
        probability, identity, row, _cell, history = item
        positive = probability > 0.0
        return (
            0 if positive else 1,
            -probability if positive else 0.0,
            0 if positive else max(0, int(finite(
                history.get("terminal_orders"), 0.0))),
            0 if positive else max(0, int(finite(
                history.get("no_opposite_flow"), 0.0))),
            0 if positive else max(0, int(finite(
                history.get("last_terminal_ts_ms"), 0.0))),
            -finite(row.get("selection_score"), 0.0),
            identity,
        )

    probability, _identity, row, cell, _history = sorted(
        candidates, key=rank)[0]
    cell = _annotate_exact_cell_evidence(row, cell, exact_cell_evidence)
    cell["control_selection_reason"] = (
        "FRESH_CAUSAL_PROJECTED_FILL" if probability > 0.0
        else "MINIMUM_EXACT_CELL_TERMINAL_ATTEMPTS"
    )
    row["control_exploration_authorized"] = True
    row["execution_role"] = (
        "POSITIVE_FLOW_CONTROL" if probability > 0.0 else "COLD_START_CONTROL")
    row["authorized_execution_cells"] = [cell]
    row["authorized_execution_cell_count"] = 1
    return 1


def _annotate_inventory_seed_authority(rows: list[dict[str, Any]]) -> None:
    """Seed token inventory only where an authorized ask can consume it.

    Observation lanes and bid-only cells need no complete-set seed. This lets
    the process subscribe a broad universe without immobilizing capital in
    zero-flow markets.
    """
    for row in rows:
        cells = row.get("authorized_execution_cells")
        row["inventory_seed_authorized"] = bool(
            isinstance(cells, list)
            and any(isinstance(cell, dict)
                    and str(cell.get("quote_side") or "").upper() == "SELL"
                    for cell in cells)
        )


def finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def _decayed_opposite_flow_rate(
    *, shares_10m: float, prints_10m: int, prints_30s: int,
    age_ms: int, half_life_seconds: float,
) -> tuple[float, float]:
    if shares_10m <= 0.0 or prints_10m <= 0 or age_ms < 0:
        return 0.0, 0.0
    long_rate = shares_10m / 600.0
    recent_rate = shares_10m / prints_10m * max(0, prints_30s) / 30.0
    freshness = math.exp(
        -math.log(2.0) * age_ms / (max(0.1, half_life_seconds) * 1_000.0)
    )
    return max(long_rate, recent_rate) * freshness, freshness


def _decayed_opposite_print_rate(
    *, prints_10m: int, prints_30s: int, age_ms: int,
    half_life_seconds: float,
) -> float:
    """Estimate future print arrivals without treating print size as intensity."""
    if prints_10m <= 0 or age_ms < 0:
        return 0.0
    freshness = math.exp(
        -math.log(2.0) * age_ms / (max(0.1, half_life_seconds) * 1_000.0)
    )
    return max(prints_10m / 600.0, max(0, prints_30s) / 30.0) * freshness


ANCHOR_FLOW_SCHEMA = "polymarket_v7_maker_fillability_flow_snapshot_v1"


def _anchor_observed_flow_authority(
    path: Path | None, *, market_id: str, yes_token: str, no_token: str,
    books: dict[str, dict[str, Any]], selection_cfg: dict[str, Any],
    model_sha: str, now_ms: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Build exact BTC-M5 flow authority from the zero-authority WS observer."""
    if path is None or not path.is_file():
        return [], [], {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], [], {}
    flow_cfg = selection_cfg.get("recent_flow") if isinstance(
        selection_cfg.get("recent_flow"), dict) else {}
    anchor_cfg = selection_cfg.get("settlement_anchor") if isinstance(
        selection_cfg.get("settlement_anchor"), dict) else {}
    max_age_ms = max(1_000, int(float(
        flow_cfg.get("maximum_tape_age_seconds", 30.0)) * 1_000.0))
    age_ms = now_ms - int(payload.get("timestamp_ms") or 0)
    if (
        payload.get("schema") != ANCHOR_FLOW_SCHEMA
        or payload.get("model_sha") != model_sha
        or payload.get("paper_only") is not True
        or payload.get("authenticated_execution") is not False
        or payload.get("real_order_submission") is not False
        or payload.get("execution_authority") != "ZERO_AUTHORITY_RESEARCH_ONLY"
        or payload.get("evidence_complete") is not True
        or age_ms < -5_000 or age_ms > max_age_ms
        or anchor_cfg.get("causal_flow_authority_enabled") is not True
    ):
        return [], [], {}
    valid_tokens = {yes_token, no_token}
    rows = {
        str(row.get("token_id") or ""): row
        for row in payload.get("rows", [])
        if isinstance(row, dict)
        and str(row.get("market_id") or "") == market_id
        and str(row.get("token_id") or "") in valid_tokens
    }
    if set(rows) != valid_tokens:
        return [], [], {}
    minimum_2m = max(1, int(flow_cfg.get("minimum_side_prints_2m", 2)))
    minimum_10m = max(1, int(flow_cfg.get("minimum_side_prints_10m", 1)))
    maximum_side_age_ms = max(1_000, int(float(
        flow_cfg.get("maximum_last_side_age_seconds", 60.0)) * 1_000.0))
    quote_horizon = max(0.1, float(
        flow_cfg.get("selection_quote_horizon_seconds", 5.0)))
    quote_shares = max(1e-6, float(flow_cfg.get("selection_quote_shares", 5.0)))
    half_life = max(0.1, float(
        flow_cfg.get("selection_flow_half_life_seconds", 30.0)))
    minimum_fill = min(1.0, max(0.0, float(
        flow_cfg.get("rotation_min_projected_fill_probability", 0.004))))
    quotes: list[dict[str, Any]] = []
    cells: list[dict[str, Any]] = []
    buy_counts = [0, 0, 0, 0]
    sell_counts = [0, 0, 0, 0]
    buy_shares_10m = 0.0
    sell_shares_10m_total = 0.0
    buy_ages: list[int] = []
    sell_ages: list[int] = []
    for outcome, token in (("YES", yes_token), ("NO", no_token)):
        row = rows[token]
        book = books.get(token)
        if not isinstance(book, dict):
            return [], [], {}
        sell_5s = int(row.get("sell_prints_5s") or 0)
        sell_30s = int(row.get("sell_prints_30s") or 0)
        sell_2m = int(row.get("sell_prints_120s") or 0)
        sell_10m = int(row.get("sell_prints_600s") or 0)
        sell_shares_2m = max(0.0, finite(row.get("sell_shares_120s")))
        sell_shares_10m = max(0.0, finite(row.get("sell_shares_600s")))
        last_sell = int(row.get("last_sell_receive_ms") or 0)
        sell_age = now_ms - last_sell if last_sell > 0 else -1
        if sell_age >= 0:
            sell_ages.append(sell_age)
        flow_rate, freshness = _decayed_opposite_flow_rate(
            shares_10m=sell_shares_10m,
            prints_10m=sell_10m,
            prints_30s=sell_30s,
            age_ms=sell_age,
            half_life_seconds=half_life,
        )
        print_rate = _decayed_opposite_print_rate(
            prints_10m=sell_10m,
            prints_30s=sell_30s,
            age_ms=sell_age,
            half_life_seconds=half_life,
        )
        expected_prints = print_rate * quote_horizon
        expected_shares = flow_rate * quote_horizon
        reach = 1.0 - math.exp(-expected_prints) if expected_prints > 0.0 else 0.0
        conditional = expected_shares / reach if reach > 1e-12 else 0.0
        queue_ahead = max(0.0, float(book["best_bid_size"]))
        join_queue = min(
            1.0, conditional / max(1e-9, queue_ahead + quote_shares))
        join_fill = reach * join_queue
        tick = float(book["tick_size"])
        inside_ticks = max(0, int(round(
            (float(book["best_ask"]) - float(book["best_bid"])) / tick)) - 1)
        improve_available = inside_ticks >= 1
        improve_fill = reach if improve_available else 0.0
        fresh = (
            sell_2m >= minimum_2m
            and sell_10m >= minimum_10m
            and 0 <= sell_age <= maximum_side_age_ms
        )
        actions: list[dict[str, Any]] = []
        for action, queue_probability, fill_probability, available in (
            ("JOIN", join_queue, join_fill, True),
            ("IMPROVE1", 1.0, improve_fill, improve_available),
        ):
            if fresh and available and fill_probability + 1e-12 >= minimum_fill:
                authority = {
                    "action": action,
                    "authority_basis": "FRESH_OPPOSITE_FLOW",
                    "projected_flow_reach_probability": reach,
                    "projected_queue_depletion_probability": queue_probability,
                    "projected_fill_probability": fill_probability,
                    "fill_probability_source": "ANCHOR_CAUSAL_WS_FLOW",
                }
                actions.append(authority)
                cells.append({
                    "outcome": outcome,
                    "token_id": token,
                    "quote_side": "BUY",
                    **authority,
                })
        quotes.append({
            "outcome": outcome,
            "token_id": token,
            "quote_side": "BUY",
            "required_aggressor_side": "SELL",
            "book_evidence_valid": True,
            "opposite_flow_is_fresh": fresh,
            "opposite_prints_30s": sell_30s,
            "opposite_prints_2m": sell_2m,
            "opposite_prints_10m": sell_10m,
            "opposite_shares_2m": sell_shares_2m,
            "opposite_shares_10m": sell_shares_10m,
            "last_opposite_flow_age_ms": sell_age,
            "opposite_flow_freshness": freshness,
            "opposite_flow_shares_per_second": flow_rate,
            "opposite_flow_prints_per_second": print_rate,
            "expected_opposite_prints_at_horizon": expected_prints,
            "expected_opposite_shares_at_horizon": expected_shares,
            "conditional_opposite_shares_given_reach": conditional,
            "tick_size": tick,
            "best_bid": float(book["best_bid"]),
            "best_ask": float(book["best_ask"]),
            "queue_ahead_shares": queue_ahead,
            "inside_ticks": inside_ticks,
            "improve1_available": improve_available,
            "projected_flow_reach_probability": reach,
            "projected_join_queue_depletion_probability": join_queue,
            "projected_join_fill_probability": join_fill,
            "projected_improve1_fill_probability": improve_fill,
            "projected_best_fill_probability": max(join_fill, improve_fill),
            "authorized_actions": actions,
            "flow_source": "ANCHOR_CAUSAL_WS_FLOW",
        })
        sell_counts[0] += sell_5s
        sell_counts[1] += sell_30s
        sell_counts[2] += sell_2m
        sell_counts[3] += sell_10m
        sell_shares_10m_total += sell_shares_10m
        for idx, key in enumerate((
            "buy_prints_5s", "buy_prints_30s", "buy_prints_120s", "buy_prints_600s")):
            buy_counts[idx] += int(row.get(key) or 0)
        buy_shares_10m += max(0.0, finite(row.get("buy_shares_600s")))
        last_buy = int(row.get("last_buy_receive_ms") or 0)
        if last_buy > 0:
            buy_ages.append(now_ms - last_buy)
    fields = {
        "recent_prints": buy_counts[3] + sell_counts[3],
        "recent_unique_transactions": 0,
        "recent_share_volume": buy_shares_10m + sell_shares_10m_total,
        "recent_notional_usd": 0.0,
        "recent_flow_to_liquidity": 0.0,
        "recent_last_trade_age_ms": min(
            [age for age in buy_ages + sell_ages if age >= 0], default=-1),
        "recent_buy_prints_5s": buy_counts[0],
        "recent_buy_prints_30s": buy_counts[1],
        "recent_buy_prints_2m": buy_counts[2],
        "recent_buy_prints_10m": buy_counts[3],
        "recent_buy_share_volume_10m": buy_shares_10m,
        "recent_buy_notional_usd_10m": 0.0,
        "recent_last_buy_age_ms": min(buy_ages) if buy_ages else -1,
        "recent_sell_prints_5s": sell_counts[0],
        "recent_sell_prints_30s": sell_counts[1],
        "recent_sell_prints_2m": sell_counts[2],
        "recent_sell_prints_10m": sell_counts[3],
        "recent_sell_share_volume_10m": sell_shares_10m_total,
        "recent_sell_notional_usd_10m": 0.0,
        "recent_last_sell_age_ms": min(sell_ages) if sell_ages else -1,
    }
    return cells, quotes, fields


def request_json(url: str, *, timeout: float = 20.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "polymarket-v7-maker/1"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _settlement_anchor_book(
    raw: Any, *, token_id: str, receive_ms: int, maximum_clock_skew_ms: int,
) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or str(raw.get("asset_id") or "") != token_id:
        return None
    bids: list[tuple[float, float]] = []
    asks: list[tuple[float, float]] = []
    for key, output in (("bids", bids), ("asks", asks)):
        rows = raw.get(key) if isinstance(raw.get(key), list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            price = finite(row.get("price"), -1.0)
            size = finite(row.get("size"), 0.0)
            if 0.0 < price < 1.0 and size > 0.0:
                output.append((price, size))
    bids.sort(reverse=True)
    asks.sort()
    if not bids or not asks or not 0.0 < bids[0][0] < asks[0][0] < 1.0:
        return None
    exchange_ms = int(finite(raw.get("timestamp"), 0.0))
    if 0 < exchange_ms < 10_000_000_000:
        exchange_ms *= 1_000
    if (
        exchange_ms <= 0
        or exchange_ms > receive_ms + max(0, maximum_clock_skew_ms)
    ):
        return None
    tick = finite(raw.get("tick_size"), 0.0)
    tick_e4 = int(round(tick * 10_000.0))
    if (
        not 0.0 < tick < 1.0
        or tick_e4 <= 0
        or 10_000 % tick_e4 != 0
        or abs(tick - tick_e4 / 10_000.0) > 1e-9
    ):
        return None
    return {
        "token_id": token_id,
        "best_bid": bids[0][0],
        "best_ask": asks[0][0],
        "best_bid_size": bids[0][1],
        "best_ask_size": asks[0][1],
        "tick_size": tick,
        "min_order_size": max(0.0, finite(raw.get("min_order_size"), 0.0)),
        "exchange_timestamp_ms": min(exchange_ms, receive_ms),
        "receive_timestamp_ms": receive_ms,
        "snapshot_id": str(raw.get("hash") or ""),
    }


def _settlement_anchor_global_fill_probability(
    execution_model_path: Path | None, *, model_sha: str,
) -> tuple[float, str]:
    if execution_model_path is None or not execution_model_path.is_file():
        return 0.0, "MODEL_MISSING"
    try:
        model = json.loads(execution_model_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0.0, "MODEL_INVALID"
    groups = model.get("groups") if isinstance(model.get("groups"), dict) else {}
    global_group = groups.get("GLOBAL") if isinstance(groups.get("GLOBAL"), dict) else {}
    probability = finite(global_group.get("fill_probability"), -1.0)
    if (
        model.get("schema") != EXECUTION_MODEL_SCHEMA
        or model.get("paper_only") is not True
        or model.get("authenticated_execution") is not False
        or model.get("real_order_submission") is not False
        or model.get("model_sha") != model_sha
        or not 0.0 < probability <= 1.0
    ):
        return 0.0, "MODEL_NOT_CAUSALLY_USABLE"
    orders = max(0, int(finite(global_group.get("orders"), 0.0)))
    return probability, (
        "EXECUTION_MODEL_GLOBAL_POSTERIOR"
        if orders > 0 else "EXECUTION_MODEL_COLD_PRIOR"
    )


def _inject_settlement_anchor(
    snapshot: dict[str, Any], *,
    fair_status_path: Path | None,
    universe_path: Path | None,
    execution_model_path: Path | None,
    anchor_flow_path: Path | None = None,
    selection_cfg: dict[str, Any],
    model_sha: str,
    now_ms: int,
    now_monotonic_ns: int | None = None,
    request_fn: Callable[..., Any] = request_json,
) -> dict[str, Any]:
    """Reserve one existing maker observation slot for the verified BTC M5 contract.

    The anchor is PAPER research authority only.  It neither increases resource
    capacity nor bypasses the selector/executor contracts.  A public CLOB book
    and the execution model's own GLOBAL posterior are required before a JOIN
    control cell is published.
    """
    anchor_cfg = selection_cfg.get("settlement_anchor")
    result = snapshot
    result["settlement_anchor_state"] = "DISABLED"
    result["settlement_anchor_authorized"] = False
    result["settlement_anchor_preserved_flow_authority"] = False
    result["settlement_anchor_observed_flow_authority"] = False
    result["settlement_anchor_market_id"] = ""
    result["settlement_anchor_evicted_market_id"] = ""
    result["settlement_anchor_fill_probability_source"] = ""
    result["settlement_anchor_identity_source"] = ""
    if not isinstance(anchor_cfg, dict) or anchor_cfg.get("enabled") is not True:
        return result
    result["settlement_anchor_state"] = "AWAITING_INPUT"
    if fair_status_path is None or universe_path is None:
        return result
    try:
        fair_status = json.loads(fair_status_path.read_text(encoding="utf-8"))
        universe = json.loads(universe_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        result["settlement_anchor_state"] = "INPUT_INVALID"
        return result
    fair = fair_status.get("fair") if isinstance(fair_status.get("fair"), dict) else {}
    market = fair_status.get("market") if isinstance(fair_status.get("market"), dict) else {}
    contract = fair_status.get("contract") if isinstance(fair_status.get("contract"), dict) else {}
    reference = fair_status.get("settlement_reference") if isinstance(
        fair_status.get("settlement_reference"), dict) else {}
    oracle = fair_status.get("oracle") if isinstance(fair_status.get("oracle"), dict) else {}
    external = fair_status.get("external") if isinstance(fair_status.get("external"), dict) else {}
    current_mono = time.monotonic_ns() if now_monotonic_ns is None else int(now_monotonic_ns)
    calculated_mono = int(finite(fair.get("calculated_monotonic_ns"), 0.0))
    valid_until_mono = int(finite(fair.get("valid_until_monotonic_ns"), 0.0))
    slug = str(market.get("slug") or "")
    market_id = str(market.get("market_id") or "")
    condition_id = str(market.get("condition_id") or "")
    event_id = str(market.get("event_id") or "")
    question = str(market.get("question") or "")
    yes_token = str(market.get("yes_token") or "")
    no_token = str(market.get("no_token") or "")
    fair_yes = finite(fair.get("yes"), -1.0)
    fee_schedule = market.get("fee_schedule") if isinstance(
        market.get("fee_schedule"), dict) else {}
    fee_rate = finite(fee_schedule.get("rate"), -1.0)
    fee_exponent = finite(fee_schedule.get("exponent"), -1.0)
    maker_fee_safe = (
        fee_rate >= 0.0
        and fee_exponent >= 0.0
        and fee_schedule.get("takerOnly") is True
    )
    research_fair_ready = (
        fair.get("research_model") is True
        and fair.get("research_model_state") == "FROZEN_INFERENCE_ONLY"
        and fair.get("real_money_authority") is False
        and fair.get("authority") == "SHADOW"
        and (
            fair.get("probability_interval_validated") is True
            or is_paper_learning_fair(fair, model_sha)
        )
    )
    structural_fallback_ready = (
        fair.get("paper_exploration_bootstrap") is True
        and fair.get("inference_state") == "VALID_PAPER_EXPLORATION_BOOTSTRAP"
        and fair.get("calibration_state") == "PAPER_EXPLORATION_BOOTSTRAP_APPLIED"
        and fair.get("probability_model_id") == "btc_m5_same_oracle_diffusion_bootstrap_v1"
        and fair.get("research_only") is True
        and fair.get("real_money_authority") is False
        and fair.get("uses_polymarket_price_as_feature") is False
        and fair.get("authority") == "SHADOW"
    )
    fair_mode_ready = research_fair_ready or structural_fallback_ready
    fair_ready = (
        fair_status.get("schema") == "polymarket_v7_external_fair_status_v1"
        and fair_status.get("paper_only") is True
        and fair_status.get("authenticated_execution") is False
        and fair_status.get("real_order_submission") is False
        and fair_status.get("code_sha") == model_sha
        and fair_status.get("state") == "FULL_FAIR_SHADOW_OPERATIONAL"
        and contract.get("verified") is True
        and contract.get("rules_hash_recognized") is True
        and reference.get("valid") is True
        and oracle.get("healthy") is True
        and external.get("healthy") is True
        and fair.get("valid") is True
        and fair_mode_ready
        and 0.0 < fair_yes < 1.0
        and calculated_mono > 0
        and calculated_mono <= current_mono <= valid_until_mono
        and slug.startswith(str(anchor_cfg.get("required_slug_prefix") or ""))
        and market_id and condition_id and event_id
        and yes_token and no_token and yes_token != no_token
        and market.get("active") is True
        and market.get("closed") is not True
        and market.get("accepting_orders") is True
        and maker_fee_safe
    )
    if not fair_ready:
        result["settlement_anchor_state"] = "FAIR_NOT_READY"
        return result
    if (
        universe.get("schema") != UNIVERSE_SCHEMA
        or universe.get("paper_only") is not True
        or universe.get("authenticated_execution") is not False
        or universe.get("real_order_submission") is not False
        or universe.get("execution_authority") is not False
        or universe.get("discovery_exhaustive") is not True
        or universe.get("pagination_loop_guard_hit") is not False
        or universe.get("model_sha") != model_sha
    ):
        result["settlement_anchor_state"] = "UNIVERSE_NOT_READY"
        return result
    universe_matches = [
        row for row in (universe.get("markets") if isinstance(universe.get("markets"), list) else [])
        if isinstance(row, dict) and str(row.get("market_id") or "") == market_id
    ]
    if universe_matches:
        if len(universe_matches) != 1:
            result["settlement_anchor_state"] = "UNIVERSE_IDENTITY_CONFLICT"
            return result
        universe_row = universe_matches[0]
        tokens = universe_row.get("clob_token_ids") if isinstance(
            universe_row.get("clob_token_ids"), list) else []
        events = universe_row.get("event_ids") if isinstance(
            universe_row.get("event_ids"), list) else []
        universe_identity_ok = (
            str(universe_row.get("condition_id") or "") == condition_id
            and len(tokens) >= 2
            and str(tokens[0]) == yes_token and str(tokens[1]) == no_token
            and events and str(events[0]) == event_id
            and universe_row.get("active") is True
            and universe_row.get("closed") is not True
            and universe_row.get("accepting_orders") is True
        )
        if not universe_identity_ok:
            result["settlement_anchor_state"] = "UNIVERSE_IDENTITY_CONFLICT"
            return result
        identity_row = universe_row
        identity_source = "CRYPTO_UNIVERSE"
    else:
        # The settlement monitor deliberately has a targeted exact-slug binding
        # for the current 5m contract because the generic universe can filter a
        # still-open market below its 24h-volume floor.  Reuse that already
        # verified binding rather than creating a second Gamma discovery owner.
        identity_row = {
            "condition_id": condition_id,
            "market_id": market_id,
            "event_ids": [event_id],
            "question": question,
            "slug": slug,
            "clob_token_ids": [yes_token, no_token],
            "liquidity": max(0.0, finite(market.get("liquidity"))),
            "volume_24h": max(0.0, finite(market.get("volume_24h"))),
            "active": True,
            "closed": False,
            "accepting_orders": True,
            "fee_schedule": fee_schedule,
            "fees_enabled": market.get("fees_enabled") is True,
            "fees_enabled_explicit": market.get("fees_enabled_explicit") is True,
        }
        identity_source = "VERIFIED_SETTLEMENT_BINDING"
    fill_probability, fill_source = _settlement_anchor_global_fill_probability(
        execution_model_path, model_sha=model_sha,
    )
    if fill_probability <= 0.0:
        result["settlement_anchor_state"] = "FILL_PRIOR_NOT_READY"
        return result
    clob_url = str(anchor_cfg.get("clob_url") or "").rstrip("/")
    timeout = finite(anchor_cfg.get("request_timeout_seconds"), 4.0)
    skew = int(anchor_cfg.get("maximum_clock_skew_ms") or 0)
    books: dict[str, dict[str, Any]] = {}
    try:
        for token in (yes_token, no_token):
            received = time.time_ns() // 1_000_000
            raw = request_fn(
                f"{clob_url}/book?token_id={urllib.parse.quote(token)}",
                timeout=timeout,
            )
            book = _settlement_anchor_book(
                raw, token_id=token, receive_ms=received,
                maximum_clock_skew_ms=skew,
            )
            if book is None:
                raise ValueError("book_invalid")
            books[token] = book
    except Exception:
        result["settlement_anchor_state"] = "CLOB_BOOK_NOT_READY"
        return result
    minimum_edge = finite(anchor_cfg.get("minimum_point_edge_per_share"), 0.005)
    fair_points = {yes_token: ("YES", fair_yes), no_token: ("NO", 1.0 - fair_yes)}
    choices: list[tuple[float, str, str, dict[str, Any]]] = []
    quote_opportunities: list[dict[str, Any]] = []
    for token in (yes_token, no_token):
        outcome, fair_point = fair_points[token]
        book = books[token]
        edge = fair_point - float(book["best_bid"])
        quote = {
            "outcome": outcome,
            "token_id": token,
            "quote_side": "BUY",
            "required_aggressor_side": "SELL",
            "book_evidence_valid": True,
            "opposite_flow_is_fresh": False,
            "opposite_prints_30s": 0,
            "opposite_prints_2m": 0,
            "opposite_prints_10m": 0,
            "opposite_shares_2m": 0.0,
            "opposite_shares_10m": 0.0,
            "last_opposite_flow_age_ms": -1,
            "opposite_flow_freshness": 0.0,
            "opposite_flow_shares_per_second": 0.0,
            "opposite_flow_prints_per_second": 0.0,
            "expected_opposite_prints_at_horizon": 0.0,
            "expected_opposite_shares_at_horizon": 0.0,
            "conditional_opposite_shares_given_reach": 0.0,
            "market_side_score": edge,
            "tick_size": float(book["tick_size"]),
            "best_bid": float(book["best_bid"]),
            "best_ask": float(book["best_ask"]),
            "queue_ahead_shares": float(book["best_bid_size"]),
            "inside_ticks": max(0, int(round(
                (float(book["best_ask"]) - float(book["best_bid"]))
                / float(book["tick_size"])
            )) - 1),
            "improve1_available": False,
            "projected_flow_reach_probability": 0.0,
            "projected_join_queue_depletion_probability": 0.0,
            "projected_join_fill_probability": fill_probability,
            "projected_improve1_fill_probability": 0.0,
            "projected_best_fill_probability": fill_probability,
            "fill_probability_source": fill_source,
            "settlement_point_edge_per_share": edge,
            "clob_book_receive_timestamp_ms": int(book["receive_timestamp_ms"]),
            "clob_book_exchange_timestamp_ms": int(book["exchange_timestamp_ms"]),
            "clob_book_snapshot_id": str(book["snapshot_id"]),
            "authorized_actions": [],
        }
        quote_opportunities.append(quote)
        if edge + 1e-12 >= minimum_edge:
            choices.append((edge, token, outcome, quote))
    if not choices:
        result["settlement_anchor_state"] = "NO_POSITIVE_POINT_EDGE"
        return result
    choices.sort(key=lambda item: (-item[0], item[1]))
    edge, chosen_token, chosen_outcome, _ = choices[0]
    markets = result.get("markets") if isinstance(result.get("markets"), list) else []
    existing_index = next((
        index for index, row in enumerate(markets)
        if isinstance(row, dict) and str(row.get("market_id") or "") == market_id
    ), None)
    existing_row = markets[existing_index] if existing_index is not None else None
    preserved_cells, preserved_quotes, preserved_flow_fields = _preserved_anchor_flow(
        existing_row, yes_token=yes_token, no_token=no_token,
    )
    observed_cells, observed_quotes, observed_flow_fields = _anchor_observed_flow_authority(
        anchor_flow_path,
        market_id=market_id,
        yes_token=yes_token,
        no_token=no_token,
        books=books,
        selection_cfg=selection_cfg,
        model_sha=model_sha,
        now_ms=now_ms,
    )
    existing_controls = sum(
        1 for row in result.get("markets", [])
        if isinstance(row, dict) and row.get("control_exploration_authorized") is True
    )
    maximum_controls = min(
        int(anchor_cfg.get("maximum_control_markets") or 0),
        int((selection_cfg.get("recent_flow") or {}).get(
            "control_exploration_maximum_markets", 0) or 0),
    )
    can_authorize = (
        not preserved_cells
        and not observed_cells
        and anchor_cfg.get("execution_authority_enabled") is True
        and existing_controls < maximum_controls
    )
    cell = {
        "outcome": chosen_outcome,
        "token_id": chosen_token,
        "action": "JOIN",
        "quote_side": "BUY",
        "authority_basis": "SETTLEMENT_ANCHOR_COLD_START_CONTROL",
        "projected_flow_reach_probability": 0.0,
        "projected_queue_depletion_probability": 0.0,
        "projected_fill_probability": fill_probability,
        "fill_probability_source": fill_source,
        "settlement_point_edge_per_share": edge,
    }
    exact_evidence = _load_exact_cell_evidence(
        execution_model_path, model_sha=model_sha,
    )
    flow_cells: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for flow_cell in preserved_cells + observed_cells:
        identity = _exact_cell_identity(
            market_id, flow_cell.get("token_id"),
            flow_cell.get("action"), flow_cell.get("quote_side"),
        )
        flow_cells[identity] = flow_cell
    if flow_cells:
        execution_cells = [
            _annotate_exact_cell_evidence(
                {"market_id": market_id}, flow_cell, exact_evidence
            )
            for _, flow_cell in sorted(flow_cells.items())
        ]
    elif can_authorize:
        execution_cells = [
            _annotate_exact_cell_evidence(
                {"market_id": market_id}, cell, exact_evidence
            )
        ]
    else:
        execution_cells = []
    quote_by_identity: dict[tuple[str, str, str], dict[str, Any]] = {}
    for quote in quote_opportunities:
        identity = (
            str(quote.get("token_id") or ""),
            str(quote.get("outcome") or "").upper(),
            str(quote.get("quote_side") or "").upper(),
        )
        quote_by_identity[identity] = dict(quote)
    for quote in preserved_quotes + observed_quotes:
        identity = (
            str(quote.get("token_id") or ""),
            str(quote.get("outcome") or "").upper(),
            str(quote.get("quote_side") or "").upper(),
        )
        quote_by_identity[identity] = {
            **quote_by_identity.get(identity, {}), **quote,
        }
    merged_quote_opportunities = [
        quote_by_identity[key] for key in sorted(quote_by_identity)
    ]
    anchor_best_fill_probability = max(
        (finite(quote.get("projected_best_fill_probability"), 0.0)
         for quote in merged_quote_opportunities),
        default=fill_probability,
    )
    preserved_control = bool(preserved_cells) and bool(
        isinstance(existing_row, dict)
        and existing_row.get("control_exploration_authorized") is True
    )
    anchor_flow_fields = dict(preserved_flow_fields)
    anchor_flow_fields.update(observed_flow_fields)
    anchor_row = {
        "condition_id": str(identity_row.get("condition_id") or ""),
        "market_id": market_id,
        "event_id": event_id,
        "slug": slug,
        "question": str(identity_row.get("question") or ""),
        "yes_token": yes_token,
        "no_token": no_token,
        "volume_24h": max(0.0, finite(identity_row.get("volume_24h"))),
        "liquidity": max(0.0, finite(identity_row.get("liquidity"))),
        "midpoint": 0.5 * (float(books[yes_token]["best_bid"]) + float(books[yes_token]["best_ask"])),
        "spread": float(books[yes_token]["best_ask"]) - float(books[yes_token]["best_bid"]),
        "market_competitiveness": 0.0,
        "rewards_max_spread_cents": 0.0,
        "rewards_min_size": 0.0,
        "native_daily_rate": 0.0,
        "sponsored_daily_rate": 0.0,
        "total_daily_rate": 0.0,
        "reward_intensity": 0.0,
        "selection_score": edge,
        "best_projected_fill_probability": anchor_best_fill_probability,
        "bid_opportunity_score": edge,
        "ask_opportunity_score": 0.0,
        "bilateral_market_making_score": 0.0,
        "complete_set_cycle_score": 0.0,
        "reward_capture_score": 0.0,
        "side_mode": "SETTLEMENT_ANCHOR",
        "quote_opportunities": merged_quote_opportunities,
        "execution_role": (
            "FLOW_AUTHORIZED" if observed_cells
            else str(existing_row.get("execution_role") or "FLOW_AUTHORIZED")
            if preserved_cells and isinstance(existing_row, dict)
            else "SETTLEMENT_ANCHOR_CONTROL" if can_authorize
            else "SETTLEMENT_ANCHOR_OBSERVATION"
        ),
        "control_exploration_authorized": preserved_control or can_authorize,
        "authorized_execution_cells": execution_cells,
        "authorized_execution_cell_count": len(execution_cells),
        "inventory_seed_authorized": False,
        "recent_prints": 0,
        "recent_unique_transactions": 0,
        "recent_share_volume": 0.0,
        "recent_notional_usd": 0.0,
        "recent_flow_to_liquidity": 0.0,
        "recent_last_trade_age_ms": -1,
        "recent_buy_prints_5s": 0,
        "recent_buy_prints_30s": 0,
        "recent_buy_prints_2m": 0,
        "recent_buy_prints_10m": 0,
        "recent_buy_share_volume_10m": 0.0,
        "recent_buy_notional_usd_10m": 0.0,
        "recent_last_buy_age_ms": -1,
        "recent_sell_prints_5s": 0,
        "recent_sell_prints_30s": 0,
        "recent_sell_prints_2m": 0,
        "recent_sell_prints_10m": 0,
        "recent_sell_share_volume_10m": 0.0,
        "recent_sell_notional_usd_10m": 0.0,
        "recent_last_sell_age_ms": -1,
        "settlement_anchor": True,
        "settlement_anchor_fair_probability": fair_yes,
        "settlement_anchor_fill_probability_source": (
            "ANCHOR_CAUSAL_WS_FLOW" if observed_cells else fill_source
        ),
        "settlement_anchor_research_only": True,
        "settlement_anchor_real_money_authority": False,
        "settlement_anchor_identity_source": identity_source,
        "fee_schedule": fee_schedule,
        "fees_enabled": market.get("fees_enabled") is True,
        "fees_enabled_explicit": market.get("fees_enabled_explicit") is True,
    }
    anchor_row.update(anchor_flow_fields)
    evicted_market_id = ""
    if existing_index is not None:
        markets[existing_index] = anchor_row
    elif len(markets) < int(result.get("resource_capacity_markets") or 0):
        markets.append(anchor_row)
    elif anchor_cfg.get("evict_observation_only_market") is True:
        evictable = [
            (finite(row.get("selection_score")), index)
            for index, row in enumerate(markets)
            if isinstance(row, dict)
            and int(row.get("authorized_execution_cell_count") or 0) == 0
            and row.get("control_exploration_authorized") is not True
            and row.get("settlement_anchor") is not True
        ]
        if not evictable:
            result["settlement_anchor_state"] = "NO_SAFE_CAPACITY_SLOT"
            return result
        _, index = min(evictable, key=lambda item: (item[0], item[1]))
        evicted_market_id = str(markets[index].get("market_id") or "")
        markets[index] = anchor_row
    else:
        result["settlement_anchor_state"] = "NO_SAFE_CAPACITY_SLOT"
        return result
    result["markets"] = markets
    result["selected_count"] = len(markets)
    result["authorized_execution_cell_count"] = sum(
        len(row.get("authorized_execution_cells") or [])
        for row in markets if isinstance(row, dict)
    )
    result["control_exploration_cell_count"] = sum(
        len(row.get("authorized_execution_cells") or [])
        for row in markets if isinstance(row, dict)
        and row.get("control_exploration_authorized") is True
    )
    result["control_exploration_market_count"] = sum(
        1 for row in markets if isinstance(row, dict)
        and row.get("control_exploration_authorized") is True
    )
    result["flow_authorized_market_count"] = sum(
        1 for row in markets if isinstance(row, dict)
        and any(
            isinstance(cell, dict)
            and str(cell.get("authority_basis") or "") in FLOW_EXECUTION_AUTHORITY_BASES
            for cell in row.get("authorized_execution_cells", [])
        )
    )
    result["unused_resource_capacity_markets"] = max(
        0, int(result.get("resource_capacity_markets") or 0) - len(markets)
    )
    result["settlement_anchor_state"] = (
        "OBSERVATION_WITH_CAUSAL_FLOW_AUTHORITY" if observed_cells
        else "OBSERVATION_WITH_PRESERVED_FLOW_AUTHORITY" if preserved_cells
        else "AUTHORIZED" if can_authorize
        else "OBSERVATION_ONLY_EXECUTION_DISABLED"
        if anchor_cfg.get("execution_authority_enabled") is not True
        else "OBSERVATION_ONLY_CONTROL_CAP"
    )
    result["settlement_anchor_authorized"] = can_authorize
    result["settlement_anchor_preserved_flow_authority"] = bool(preserved_cells)
    result["settlement_anchor_observed_flow_authority"] = bool(observed_cells)
    result["settlement_anchor_market_id"] = market_id
    result["settlement_anchor_evicted_market_id"] = evicted_market_id
    result["settlement_anchor_fill_probability_source"] = (
        "ANCHOR_CAUSAL_WS_FLOW" if observed_cells else fill_source
    )
    result["settlement_anchor_identity_source"] = identity_source
    return result


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def selection_membership_sha256(snapshot: dict[str, Any]) -> str:
    markets = snapshot.get("markets") if isinstance(snapshot.get("markets"), list) else []
    membership = sorted(
        (
            str(row.get("condition_id") or ""),
            str(row.get("market_id") or ""),
            str(row.get("yes_token") or ""),
            str(row.get("no_token") or ""),
        )
        for row in markets
        if isinstance(row, dict)
    )
    payload = json.dumps(membership, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _iso_timestamp_ms(value: Any) -> int:
    raw = str(value or "").strip()
    if not raw:
        return 0
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000.0)
    except (ValueError, OverflowError):
        return 0


TRADE_TAPE_FIELDS = (
    "timestamp", "received_ms", "lag_ms", "condition_id", "asset_id", "outcome",
    "side", "price", "size", "transaction_hash", "slug", "event_slug",
)
CAUSAL_BOOK_SCHEMA = "polymarket_v7_causal_book_observation_v1"


def _tail_complete_lines(path: Path, *, maximum_bytes: int = 64 * 1024 * 1024) -> list[str]:
    """Read a bounded suffix without returning partial first/last records."""
    if not path.is_file():
        return []
    size = path.stat().st_size
    start = max(0, size - max(1, int(maximum_bytes)))
    with path.open("rb") as handle:
        handle.seek(start)
        raw = handle.read()
    if start > 0:
        cut = raw.find(b"\n")
        raw = raw[cut + 1:] if cut >= 0 else b""
    if raw and not raw.endswith(b"\n"):
        cut = raw.rfind(b"\n")
        raw = raw[:cut + 1] if cut >= 0 else b""
    return raw.decode("utf-8", errors="strict").splitlines()

def _latest_causal_books(
    book_tape_path: Path | None, *, model_sha: str, now_ms: int,
    maximum_age_ms: int,
) -> dict[str, dict[str, Any]]:
    if book_tape_path is None or not book_tape_path.is_file():
        return {}
    latest: dict[str, dict[str, Any]] = {}
    for line in _tail_complete_lines(book_tape_path):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            not isinstance(row, dict) or row.get("schema") != CAUSAL_BOOK_SCHEMA
            or row.get("model_sha") != model_sha or row.get("paper_only") is not True
            or row.get("authenticated_execution") is not False
            or row.get("real_order_submission") is not False
            or row.get("execution_authority") != "ZERO_AUTHORITY_RESEARCH_ONLY"
            or row.get("valid") is not True or row.get("lineage_continuous") is not True
        ):
            continue
        token_id = str(row.get("token_id") or "")
        receive_ms = int(finite(row.get("receive_wall_ms"), 0.0))
        age_ms = now_ms - receive_ms
        if not token_id or receive_ms <= 0 or age_ms < -5_000 or age_ms > maximum_age_ms:
            continue
        if token_id not in latest or receive_ms > int(latest[token_id]["receive_wall_ms"]):
            latest[token_id] = row
    return latest

def _empty_flow_item() -> dict[str, Any]:
    return {
        "prints": 0, "shares": 0.0, "notional": 0.0,
        "buy_prints_5s": 0, "buy_prints_30s": 0, "buy_prints_2m": 0, "buy_prints_10m": 0,
        "buy_shares_2m": 0.0, "buy_shares_10m": 0.0, "buy_notional_10m": 0.0,
        "last_buy_receive_ms": 0,
        "sell_prints_5s": 0, "sell_prints_30s": 0, "sell_prints_2m": 0, "sell_prints_10m": 0,
        "sell_shares_2m": 0.0, "sell_shares_10m": 0.0, "sell_notional_10m": 0.0,
        "last_sell_receive_ms": 0, "last_receive_ms": 0,
        "transactions": set(), "token_flow": {},
    }


def _empty_token_flow() -> dict[str, Any]:
    return {
        "buy_prints_5s": 0, "buy_prints_30s": 0, "buy_prints_2m": 0, "buy_prints_10m": 0,
        "buy_shares_2m": 0.0, "buy_shares_10m": 0.0, "buy_notional_10m": 0.0,
        "last_buy_receive_ms": 0,
        "sell_prints_5s": 0, "sell_prints_30s": 0, "sell_prints_2m": 0, "sell_prints_10m": 0,
        "sell_shares_2m": 0.0, "sell_shares_10m": 0.0, "sell_notional_10m": 0.0,
        "last_sell_receive_ms": 0,
        "tick_size": 0.0, "best_bid": 0.0, "best_ask": 0.0,
        "best_bid_depth": 0.0, "best_ask_depth": 0.0, "book_evidence_valid": False,
    }

def _canonical_trade_tape_aggregates(
    trade_tape_path: Path, *, model_sha: str, now_ms: int,
    maximum_age_ms: int, book_tape_path: Path | None = None,
) -> tuple[dict[str, dict[str, Any]], int]:
    """Aggregate causal public trades from the configured crypto universe only."""
    if not trade_tape_path.is_file():
        raise ValueError("maker_trade_tape_missing")
    status_path = trade_tape_path.with_name("trade_recorder_status.json")
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("maker_trade_recorder_status_missing") from error
    status_age_ms = now_ms - int(status.get("timestamp_ms") or 0)
    if (
        status.get("schema") != "polymarket_v7_trade_recorder_status_v1"
        or status.get("model_sha") != model_sha
        or status.get("scope") != "CONFIGURED_CRYPTO_CONTEXTS_ONLY"
        or status.get("paper_only") is not True
        or status.get("authenticated_execution") is not False
        or status.get("real_order_submission") is not False
        or status.get("data_plane_healthy") is not True
        or status_age_ms < -5_000 or status_age_ms > maximum_age_ms
    ):
        raise ValueError("maker_trade_recorder_status_invalid")
    session_start_ms = int(status.get("session_start_ms") or 0)
    if session_start_ms <= 0 or session_start_ms > now_ms + 5_000:
        raise ValueError("maker_trade_recorder_session_invalid")

    lines = _tail_complete_lines(trade_tape_path)
    if lines and lines[0].startswith("timestamp,received_ms,"):
        reader = csv.DictReader(lines)
    else:
        reader = csv.DictReader(lines, fieldnames=TRADE_TAPE_FIELDS)
    aggregates: dict[str, dict[str, Any]] = {}
    latest_receive_ms = 0
    for raw in reader:
        try:
            receive_ms = int(raw.get("received_ms") or 0)
            price = float(raw.get("price") or 0.0)
            size = float(raw.get("size") or 0.0)
        except (TypeError, ValueError, OverflowError):
            continue
        age_ms = now_ms - receive_ms
        condition_id = str(raw.get("condition_id") or "")
        token_id = str(raw.get("asset_id") or "")
        side = str(raw.get("side") or "").upper()
        if (
            receive_ms < session_start_ms or age_ms < -5_000 or age_ms > 600_000
            or not condition_id or not token_id or side not in {"BUY", "SELL"}
            or not math.isfinite(price) or not math.isfinite(size)
            or not 0.0 < price < 1.0 or size <= 0.0
        ):
            continue
        latest_receive_ms = max(latest_receive_ms, receive_ms)
        item = aggregates.setdefault(condition_id, _empty_flow_item())
        token_flow = item["token_flow"].setdefault(token_id, _empty_token_flow())
        prefix = side.lower()

        for window_ms, suffix in ((5_000, "5s"), (30_000, "30s"),
                                  (120_000, "2m"), (600_000, "10m")):
            if age_ms <= window_ms:
                item[f"{prefix}_prints_{suffix}"] += 1
                token_flow[f"{prefix}_prints_{suffix}"] += 1
        if age_ms <= 120_000:
            item[f"{prefix}_shares_2m"] += size
            token_flow[f"{prefix}_shares_2m"] += size
        item[f"{prefix}_shares_10m"] += size
        token_flow[f"{prefix}_shares_10m"] += size
        item[f"{prefix}_notional_10m"] += price * size
        token_flow[f"{prefix}_notional_10m"] += price * size
        item[f"last_{prefix}_receive_ms"] = max(int(item[f"last_{prefix}_receive_ms"]), receive_ms)
        token_flow[f"last_{prefix}_receive_ms"] = max(
            int(token_flow[f"last_{prefix}_receive_ms"]), receive_ms)
        item["last_receive_ms"] = max(int(item["last_receive_ms"]), receive_ms)
        tx_id = str(raw.get("transaction_hash") or "")
        if not tx_id:
            tx_id = f"{condition_id}:{token_id}:{receive_ms}:{side}:{price:.12g}:{size:.12g}"
        item["transactions"].add(tx_id)

    for item in aggregates.values():
        item["prints"] = int(item["buy_prints_10m"]) + int(item["sell_prints_10m"])
        item["shares"] = float(item["buy_shares_10m"]) + float(item["sell_shares_10m"])
        item["notional"] = float(item["buy_notional_10m"]) + float(item["sell_notional_10m"])

    books = _latest_causal_books(
        book_tape_path, model_sha=model_sha, now_ms=now_ms,
        maximum_age_ms=maximum_age_ms,
    )
    for item in aggregates.values():
        for token_id, token_flow in item["token_flow"].items():
            book = books.get(token_id)
            if not isinstance(book, dict):
                continue
            tick_size = max(0.0, finite(book.get("tick_size"), 0.0))
            best_bid = max(0.0, finite(book.get("best_bid"), 0.0))
            best_ask = min(1.0, finite(book.get("best_ask"), 1.0))
            best_bid_depth = max(0.0, finite(book.get("bid_depth_l1"), 0.0))
            best_ask_depth = max(0.0, finite(book.get("ask_depth_l1"), 0.0))
            if (
                tick_size > 0.0 and 0.0 < best_bid < best_ask < 1.0
                and best_bid_depth > 0.0 and best_ask_depth > 0.0
            ):
                token_flow.update({
                    "tick_size": tick_size, "best_bid": best_bid, "best_ask": best_ask,
                    "best_bid_depth": best_bid_depth, "best_ask_depth": best_ask_depth,
                    "book_evidence_valid": True,
                })
    if latest_receive_ms <= 0 or now_ms - latest_receive_ms > maximum_age_ms:
        raise ValueError(f"maker_trade_tape_stale:{now_ms - latest_receive_ms}")
    return aggregates, latest_receive_ms

def _recent_flow_snapshot(
    universe_path: Path,
    selection_cfg: dict[str, Any],
    capacity_cfg: dict[str, Any],
    resource_capacity: int,
    *,
    model_sha: str,
    now_ms: int,
    trade_tape_path: Path,
    book_tape_path: Path | None = None,
    exact_cell_evidence: dict[
        tuple[str, str, str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Rank PAPER markets by causal recent public prints, not reward-pool size."""
    flow_cfg = selection_cfg.get("recent_flow")
    if not isinstance(flow_cfg, dict) or flow_cfg.get("enabled") is not True:
        raise ValueError("maker_recent_flow_disabled")
    if not trade_tape_path.is_file():
        raise ValueError("maker_recent_flow_trade_tape_missing")
    universe = json.loads(universe_path.read_text(encoding="utf-8"))
    if (
        universe.get("schema") != UNIVERSE_SCHEMA
        or universe.get("paper_only") is not True
        or universe.get("authenticated_execution") is not False
        or universe.get("real_order_submission") is not False
        or universe.get("execution_authority") is not False
        or universe.get("discovery_exhaustive") is not True
        or universe.get("pagination_loop_guard_hit") is not False
        or universe.get("model_sha") != model_sha
    ):
        raise ValueError("maker_recent_flow_universe_contract_invalid")
    maximum_universe_age_ms = int(
        float(selection_cfg.get("fallback_universe_max_age_seconds", 180.0)) * 1000.0
    )
    universe_age_ms = now_ms - int(universe.get("timestamp_ms") or 0)
    if universe_age_ms < -5_000 or universe_age_ms > maximum_universe_age_ms:
        raise ValueError(f"maker_recent_flow_universe_stale:{universe_age_ms}")

    lookback_ms = max(1_000, int(float(flow_cfg.get("lookback_seconds", 180.0)) * 1000.0))
    maximum_tape_age_ms = max(
        1_000, int(float(flow_cfg.get("maximum_tape_age_seconds", 30.0)) * 1000.0)
    )
    aggregates, latest_receive_ms = _canonical_trade_tape_aggregates(
        trade_tape_path, model_sha=model_sha, now_ms=now_ms,
        maximum_age_ms=maximum_tape_age_ms, book_tape_path=book_tape_path,
    )
    flow_source = "CRYPTO_TRADE_TAPE"

    minimum_prints = max(1, int(flow_cfg.get("minimum_prints", 2)))
    minimum_side_prints_2m = max(1, int(flow_cfg.get(
        "minimum_side_prints_2m", flow_cfg.get("minimum_sell_prints_2m", 1))))
    minimum_side_prints_10m = max(1, int(flow_cfg.get(
        "minimum_side_prints_10m", flow_cfg.get("minimum_sell_prints_10m", 1))))
    maximum_last_side_age_ms = max(
        1_000,
        int(float(flow_cfg.get(
            "maximum_last_side_age_seconds",
            flow_cfg.get("maximum_last_sell_age_seconds", 120.0))) * 1_000.0),
    )
    maximum_market_flow_age_ms = max(
        1_000,
        int(float(flow_cfg.get("maximum_market_flow_age_seconds", 120.0)) * 1_000.0),
    )
    minimum_markets = max(1, int(flow_cfg.get("minimum_markets", 5)))
    minimum_tte_ms = max(
        0, int(float(flow_cfg.get("minimum_time_to_end_seconds", 900.0)) * 1000.0)
    )
    minimum_volume = float(selection_cfg.get("min_volume_24h", 100.0))
    minimum_liquidity = float(selection_cfg.get("min_liquidity", 25.0))
    minimum_mid = float(flow_cfg.get("minimum_mid", selection_cfg.get("min_mid", 0.02)))
    maximum_mid = float(flow_cfg.get("maximum_mid", selection_cfg.get("max_mid", 0.98)))
    maximum_spread = max(0.0, float(flow_cfg.get("maximum_spread", 0.10)))
    selection_quote_horizon_seconds = max(
        0.1, float(flow_cfg.get("selection_quote_horizon_seconds", 5.0)))
    selection_quote_shares = max(
        1e-6, float(flow_cfg.get("selection_quote_shares", 5.0)))
    selection_flow_half_life_seconds = max(
        0.1, float(flow_cfg.get("selection_flow_half_life_seconds", 30.0)))
    minimum_authorized_fill_probability = min(1.0, max(
        0.0, float(flow_cfg.get(
            "rotation_min_projected_fill_probability", 0.004))))
    weights = flow_cfg.get("score_weights") if isinstance(flow_cfg.get("score_weights"), dict) else {}
    candidates: list[dict[str, Any]] = []
    for raw in universe.get("markets") if isinstance(universe.get("markets"), list) else []:
        if not isinstance(raw, dict):
            continue
        condition_id = str(raw.get("condition_id") or "")
        flow = aggregates.get(condition_id)
        tokens = raw.get("clob_token_ids") if isinstance(raw.get("clob_token_ids"), list) else []
        events = raw.get("event_ids") if isinstance(raw.get("event_ids"), list) else []
        liquidity = max(0.0, finite(raw.get("liquidity")))
        volume_24h = max(0.0, finite(raw.get("volume_24h")))
        midpoint = finite(raw.get("midpoint"), -1.0)
        spread = max(0.0, finite(raw.get("spread")))
        end_ms = _iso_timestamp_ms(raw.get("end_date"))
        if (
            flow is None
            or int(flow["prints"]) < minimum_prints
            or now_ms - int(flow["last_receive_ms"]) > maximum_market_flow_age_ms
            or raw.get("active") is not True
            or raw.get("closed") is True
            or raw.get("accepting_orders") is not True
            or len(tokens) != 2
            or liquidity < minimum_liquidity
            or volume_24h < minimum_volume
            or midpoint < minimum_mid
            or midpoint > maximum_mid
            or spread <= 0.0
            or spread > maximum_spread
            or (minimum_tte_ms > 0 and (end_ms <= 0 or end_ms - now_ms < minimum_tte_ms))
        ):
            continue
        market_id = str(raw.get("market_id") or "")
        yes_token, no_token = str(tokens[0] or ""), str(tokens[1] or "")
        if not condition_id or not market_id or not yes_token or not no_token or yes_token == no_token:
            continue
        recent_flow_to_liquidity = float(flow["shares"]) / max(liquidity, minimum_liquidity)
        mid_balance = max(0.0, 1.0 - abs(midpoint - 0.5) / 0.5)
        buy_fresh = (
            int(flow["buy_prints_2m"]) >= minimum_side_prints_2m
            and int(flow["buy_prints_10m"]) >= minimum_side_prints_10m
            and now_ms - int(flow["last_buy_receive_ms"]) <= maximum_last_side_age_ms
        )
        sell_fresh = (
            int(flow["sell_prints_2m"]) >= minimum_side_prints_2m
            and int(flow["sell_prints_10m"]) >= minimum_side_prints_10m
            and now_ms - int(flow["last_sell_receive_ms"]) <= maximum_last_side_age_ms
        )
        common_score = (
            finite(weights.get("log_prints"), 3.0) * math.log1p(int(flow["prints"]))
            + finite(weights.get("log_notional"), 1.0) * math.log1p(float(flow["notional"]))
            + finite(weights.get("log_flow_to_liquidity"), 2.0)
              * math.log1p(recent_flow_to_liquidity * 1_000.0)
            + finite(weights.get("mid_balance"), 0.25) * mid_balance
            + finite(weights.get("log_spread_cents"), 0.10) * math.log1p(spread * 100.0)
        )
        bid_opportunity_score = common_score + (
            finite(weights.get("log_sell_prints_30s"), 4.0)
              * math.log1p(int(flow["sell_prints_30s"]))
            + finite(weights.get("log_sell_prints_2m"), 3.0)
              * math.log1p(int(flow["sell_prints_2m"]))
            + finite(weights.get("log_sell_prints_10m"), 2.0)
              * math.log1p(int(flow["sell_prints_10m"]))
            + finite(weights.get("log_sell_notional_10m"), 1.0)
              * math.log1p(float(flow["sell_notional_10m"]))
        )
        ask_opportunity_score = common_score + (
            finite(weights.get("log_buy_prints_30s"), 4.0)
              * math.log1p(int(flow["buy_prints_30s"]))
            + finite(weights.get("log_buy_prints_2m"), 3.0)
              * math.log1p(int(flow["buy_prints_2m"]))
            + finite(weights.get("log_buy_prints_10m"), 2.0)
              * math.log1p(int(flow["buy_prints_10m"]))
            + finite(weights.get("log_buy_notional_10m"), 1.0)
              * math.log1p(float(flow["buy_notional_10m"]))
        )
        bilateral_score = (
            0.5 * (bid_opportunity_score + ask_opportunity_score)
            + (2.0 if buy_fresh and sell_fresh else 0.0)
        )
        complete_set_cycle_score = bilateral_score + math.log1p(spread * 100.0)
        reward_capture_score = 0.0
        score = max(bid_opportunity_score, ask_opportunity_score, bilateral_score,
                    complete_set_cycle_score)
        side_mode = (
            "BILATERAL" if buy_fresh and sell_fresh
            else "INVENTORY_BACKED_ASK" if buy_fresh
            else "COLLATERAL_BACKED_BID" if sell_fresh
            else "STABLE_SPREAD_EXPLORATION"
        )
        token_flow = flow.get("token_flow") if isinstance(flow.get("token_flow"), dict) else {}
        quote_opportunities = []
        authorized_execution_cells: list[dict[str, Any]] = []
        for outcome, token in (("YES", yes_token), ("NO", no_token)):
            token_stats = token_flow.get(token) if isinstance(token_flow.get(token), dict) else {}
            for quote_side, aggressor_prefix, side_score in (
                ("BUY", "sell", bid_opportunity_score),
                ("SELL", "buy", ask_opportunity_score),
            ):
                opposite_shares_2m = float(token_stats.get(
                    f"{aggressor_prefix}_shares_2m", 0.0))
                opposite_shares_10m = float(token_stats.get(
                    f"{aggressor_prefix}_shares_10m", 0.0))
                opposite_prints_10m = int(token_stats.get(
                    f"{aggressor_prefix}_prints_10m", 0))
                opposite_prints_30s = int(token_stats.get(
                    f"{aggressor_prefix}_prints_30s", 0))
                last_opposite_receive_ms = int(token_stats.get(
                    f"last_{aggressor_prefix}_receive_ms", 0))
                opposite_flow_age_ms = (
                    now_ms - last_opposite_receive_ms
                    if last_opposite_receive_ms > 0 else -1
                )
                # Use the same causal rate contract consumed by the C++ maker:
                # a long-window floor, a 30-second burst term, and exponential
                # event-time decay.  Raw 120-second volume made an old burst
                # look 20-50x more fillable than the quote that was eventually
                # posted, causing queue-destroying cohort rotations.
                opposite_flow_shares_per_second, flow_freshness = (
                    _decayed_opposite_flow_rate(
                        shares_10m=opposite_shares_10m,
                        prints_10m=opposite_prints_10m,
                        prints_30s=opposite_prints_30s,
                        age_ms=opposite_flow_age_ms,
                        half_life_seconds=selection_flow_half_life_seconds,
                    )
                )
                expected_opposite_shares = (
                    opposite_flow_shares_per_second
                    * selection_quote_horizon_seconds
                )
                opposite_flow_prints_per_second = _decayed_opposite_print_rate(
                    prints_10m=opposite_prints_10m,
                    prints_30s=opposite_prints_30s,
                    age_ms=opposite_flow_age_ms,
                    half_life_seconds=selection_flow_half_life_seconds,
                )
                expected_opposite_prints = (
                    opposite_flow_prints_per_second
                    * selection_quote_horizon_seconds
                )
                # Reach is an arrival event, not a volume event. A single large
                # historical print may imply large depletion conditional on a
                # repeat, but cannot make the repeat itself almost certain.
                flow_reach_probability = (
                    1.0 - math.exp(-expected_opposite_prints)
                    if expected_opposite_prints > 0.0 else 0.0)
                conditional_opposite_shares = (
                    expected_opposite_shares / flow_reach_probability
                    if flow_reach_probability > 1e-12 else 0.0
                )
                queue_ahead = float(token_stats.get(
                    "best_bid_depth" if quote_side == "BUY" else "best_ask_depth", 0.0))
                join_queue_depletion_probability = (
                    min(1.0, conditional_opposite_shares
                        / max(1e-9, queue_ahead + selection_quote_shares))
                    if token_stats.get("book_evidence_valid") is True else 0.0)
                projected_join_fill_probability = (
                    flow_reach_probability * join_queue_depletion_probability)
                tick_size = float(token_stats.get("tick_size", 0.0))
                token_spread = max(
                    0.0, float(token_stats.get("best_ask", 0.0))
                    - float(token_stats.get("best_bid", 0.0)))
                inside_ticks = max(
                    0, int(math.floor(token_spread / tick_size + 1e-9)) - 1
                ) if tick_size > 0.0 else 0
                improve1_available = inside_ticks >= 1
                projected_improve1_fill_probability = (
                    flow_reach_probability if improve1_available else 0.0)
                projected_best_fill_probability = max(
                    projected_join_fill_probability,
                    projected_improve1_fill_probability,
                )
                opposite_flow_is_fresh = (
                    int(token_stats.get(f"{aggressor_prefix}_prints_2m", 0))
                        >= minimum_side_prints_2m
                    and opposite_prints_10m >= minimum_side_prints_10m
                    and 0 <= opposite_flow_age_ms <= maximum_last_side_age_ms
                )
                book_evidence_valid = token_stats.get("book_evidence_valid") is True
                authorized_actions: list[dict[str, Any]] = []
                for action, queue_probability, fill_probability, available in (
                    ("JOIN", join_queue_depletion_probability,
                     projected_join_fill_probability, True),
                    ("IMPROVE1", 1.0, projected_improve1_fill_probability,
                     improve1_available),
                ):
                    if (
                        opposite_flow_is_fresh
                        and book_evidence_valid
                        and available
                        and fill_probability + 1e-12
                            >= minimum_authorized_fill_probability
                    ):
                        authority = {
                            "action": action,
                            "authority_basis": "FRESH_OPPOSITE_FLOW",
                            "projected_flow_reach_probability": flow_reach_probability,
                            "projected_queue_depletion_probability": queue_probability,
                            "projected_fill_probability": fill_probability,
                        }
                        authorized_actions.append(authority)
                        authorized_execution_cells.append({
                            "outcome": outcome,
                            "token_id": token,
                            "quote_side": quote_side,
                            **authority,
                        })
                bid_depth_l1 = max(0.0, float(token_stats.get("best_bid_depth", 0.0)))
                ask_depth_l1 = max(0.0, float(token_stats.get("best_ask_depth", 0.0)))
                touch_depth = bid_depth_l1 + ask_depth_l1
                book_imbalance = (
                    (bid_depth_l1 - ask_depth_l1) / touch_depth
                    if book_evidence_valid and touch_depth > 0.0 else None
                )
                quote_opportunities.append({
                    "outcome": outcome,
                    "token_id": token,
                    "quote_side": quote_side,
                    "required_aggressor_side": aggressor_prefix.upper(),
                    "opposite_prints_30s": opposite_prints_30s,
                    "opposite_prints_2m": int(token_stats.get(
                        f"{aggressor_prefix}_prints_2m", 0)),
                    "opposite_prints_10m": opposite_prints_10m,
                    "opposite_shares_10m": opposite_shares_10m,
                    "opposite_shares_2m": opposite_shares_2m,
                    "last_opposite_flow_age_ms": opposite_flow_age_ms,
                    "opposite_flow_freshness": flow_freshness,
                    "opposite_flow_shares_per_second": (
                        opposite_flow_shares_per_second),
                    "opposite_flow_prints_per_second": (
                        opposite_flow_prints_per_second),
                    "expected_opposite_prints_at_horizon": expected_opposite_prints,
                    "expected_opposite_shares_at_horizon": expected_opposite_shares,
                    "conditional_opposite_shares_given_reach": (
                        conditional_opposite_shares),
                    "market_side_score": side_score,
                    "book_evidence_valid": book_evidence_valid,
                    "opposite_flow_is_fresh": opposite_flow_is_fresh,
                    "tick_size": tick_size,
                    "best_bid": float(token_stats.get("best_bid", 0.0)),
                    "best_ask": float(token_stats.get("best_ask", 0.0)),
                    "queue_ahead_shares": queue_ahead,
                    "book_imbalance": book_imbalance,
                    "book_imbalance_source": (
                        "CAUSAL_L1_TOUCH_DEPTH" if book_imbalance is not None
                        else "UNAVAILABLE"
                    ),
                    "inside_ticks": inside_ticks,
                    "improve1_available": improve1_available,
                    "projected_flow_reach_probability": flow_reach_probability,
                    "projected_join_queue_depletion_probability": (
                        join_queue_depletion_probability),
                    "projected_join_fill_probability": projected_join_fill_probability,
                    "projected_improve1_fill_probability": (
                        projected_improve1_fill_probability),
                    "projected_best_fill_probability": projected_best_fill_probability,
                    "authorized_actions": authorized_actions,
                })
        quote_opportunities.sort(key=lambda row: (
            -float(row["projected_best_fill_probability"]),
            -int(row["opposite_prints_30s"]),
            -int(row["opposite_prints_2m"]),
            -float(row["opposite_shares_10m"]),
            str(row["token_id"]), str(row["quote_side"]),
        ))
        best_projected_fill_probability = max(
            (float(row["projected_best_fill_probability"])
             for row in quote_opportunities), default=0.0)
        score += finite(weights.get("log_projected_fillability"), 8.0) * math.log1p(
            1_000.0 * best_projected_fill_probability)
        candidates.append({
            "condition_id": condition_id,
            "market_id": market_id,
            "event_id": str(events[0] if events else ""),
            "slug": str(raw.get("slug") or ""),
            "question": str(raw.get("question") or ""),
            "yes_token": yes_token,
            "no_token": no_token,
            "volume_24h": volume_24h,
            "liquidity": liquidity,
            "midpoint": midpoint,
            "spread": spread,
            "market_competitiveness": 0.0,
            "rewards_max_spread_cents": 0.0,
            "rewards_min_size": 0.0,
            "native_daily_rate": 0.0,
            "sponsored_daily_rate": 0.0,
            "total_daily_rate": 0.0,
            "reward_intensity": 0.0,
            "selection_score": score,
            "best_projected_fill_probability": best_projected_fill_probability,
            "bid_opportunity_score": bid_opportunity_score,
            "ask_opportunity_score": ask_opportunity_score,
            "bilateral_market_making_score": bilateral_score,
            "complete_set_cycle_score": complete_set_cycle_score,
            "reward_capture_score": reward_capture_score,
            "side_mode": side_mode,
            "quote_opportunities": quote_opportunities,
            "execution_role": (
                "FLOW_AUTHORIZED" if authorized_execution_cells
                else "WARM_FLOW_OBSERVATION"),
            "control_exploration_authorized": False,
            "authorized_execution_cells": authorized_execution_cells,
            "authorized_execution_cell_count": len(authorized_execution_cells),
            "recent_prints": int(flow["prints"]),
            "recent_unique_transactions": len(flow["transactions"]),
            "recent_share_volume": float(flow["shares"]),
            "recent_notional_usd": float(flow["notional"]),
            "recent_flow_to_liquidity": recent_flow_to_liquidity,
            "recent_last_trade_age_ms": now_ms - int(flow["last_receive_ms"]),
            "recent_buy_prints_5s": int(flow["buy_prints_5s"]),
            "recent_buy_prints_30s": int(flow["buy_prints_30s"]),
            "recent_buy_prints_2m": int(flow["buy_prints_2m"]),
            "recent_buy_prints_10m": int(flow["buy_prints_10m"]),
            "recent_buy_share_volume_10m": float(flow["buy_shares_10m"]),
            "recent_buy_notional_usd_10m": float(flow["buy_notional_10m"]),
            "recent_last_buy_age_ms": (
                now_ms - int(flow["last_buy_receive_ms"])
                if int(flow["last_buy_receive_ms"]) > 0 else -1
            ),
            "recent_sell_prints_5s": int(flow["sell_prints_5s"]),
            "recent_sell_prints_30s": int(flow["sell_prints_30s"]),
            "recent_sell_prints_2m": int(flow["sell_prints_2m"]),
            "recent_sell_prints_10m": int(flow["sell_prints_10m"]),
            "recent_sell_share_volume_10m": float(flow["sell_shares_10m"]),
            "recent_sell_notional_usd_10m": float(flow["sell_notional_10m"]),
            "recent_last_sell_age_ms": (
                now_ms - int(flow["last_sell_receive_ms"])
                if int(flow["last_sell_receive_ms"]) > 0 else -1
            ),
        })
    candidates.sort(key=lambda row: (
        -int(bool(row.get("authorized_execution_cell_count"))),
        -finite(row.get("selection_score")),
        -int(row.get("recent_prints") or 0),
        -finite(row.get("recent_notional_usd")),
        str(row.get("market_id") or ""),
    ))
    selected: list[dict[str, Any]] = []
    selected_events: set[str] = set()
    for row in candidates:
        event_key = str(row.get("event_id") or f"market:{row.get('market_id')}")
        if event_key in selected_events:
            continue
        selected_events.add(event_key)
        selected.append(row)
        if len(selected) >= resource_capacity:
            break
    # Execution remains limited to explicit cell authority, but membership is a
    # broad observation universe.  Keeping those identities warm lets authority
    # follow flow in-place without destroying current queues. Observation
    # rows carry no inventory seed and cannot emit an order.
    operational_floor = min(
        resource_capacity,
        max(minimum_markets, int(flow_cfg.get(
            "minimum_operational_markets", minimum_markets))),
    )
    maximum_zero_flow_reserve = min(
        resource_capacity,
        max(0, int(flow_cfg.get("maximum_zero_flow_reserve_markets", 0))),
    )
    observation_universe_markets = min(
        resource_capacity,
        max(operational_floor, int(flow_cfg.get(
            "observation_universe_markets", operational_floor))),
    )
    stable_reserve_added = 0
    reserve_target = min(
        observation_universe_markets,
        len(selected) + maximum_zero_flow_reserve)
    if len(selected) < reserve_target:
        reserve = _fallback_snapshot(
            universe_path, selection_cfg, capacity_cfg, resource_capacity,
            model_sha=model_sha, primary_error="recent_flow_reserve",
            now_ms=now_ms, maximum_markets=resource_capacity,
        )
        for fallback_row in reserve["markets"]:
            event_key = str(
                fallback_row.get("event_id")
                or f"market:{fallback_row.get('market_id')}"
            )
            if event_key in selected_events:
                continue
            row = dict(fallback_row)
            row.update({
                "side_mode": "STABLE_SPREAD_EXPLORATION",
                "recent_prints": 0,
                "recent_unique_transactions": 0,
                "recent_share_volume": 0.0,
                "recent_notional_usd": 0.0,
                "recent_flow_to_liquidity": 0.0,
                "recent_last_trade_age_ms": -1,
                "recent_buy_prints_5s": 0,
                "recent_buy_prints_30s": 0,
                "recent_buy_prints_2m": 0,
                "recent_buy_prints_10m": 0,
                "recent_buy_share_volume_10m": 0.0,
                "recent_buy_notional_usd_10m": 0.0,
                "recent_last_buy_age_ms": -1,
                "recent_sell_prints_5s": 0,
                "recent_sell_prints_30s": 0,
                "recent_sell_prints_2m": 0,
                "recent_sell_prints_10m": 0,
                "recent_sell_share_volume_10m": 0.0,
                "recent_sell_notional_usd_10m": 0.0,
                "recent_last_sell_age_ms": -1,
                "quote_opportunities": [],
                "execution_role": "WARM_RESERVE",
                "control_exploration_authorized": False,
                "authorized_execution_cells": [],
                "authorized_execution_cell_count": 0,
            })
            selected_events.add(event_key)
            selected.append(row)
            stable_reserve_added += 1
            if len(selected) >= reserve_target:
                break
    control_cells_authorized = _authorize_control_cells(
        selected,
        maximum_markets=max(1, int(flow_cfg.get(
            "control_exploration_maximum_markets", operational_floor))),
        minimum_prints_30s=max(1, int(flow_cfg.get(
            "control_minimum_prints_30s", 1))),
        maximum_last_side_age_ms=max(1_000, int(float(flow_cfg.get(
            "control_maximum_last_side_age_seconds", 30.0)) * 1_000.0)),
        allow_zero_flow_fallback=(
            flow_cfg.get("zero_flow_execution_fallback_enabled") is True
        ),
        exact_cell_evidence=exact_cell_evidence,
    )
    _annotate_inventory_seed_authority(selected)
    if len(selected) < minimum_markets:
        raise ValueError(f"maker_recent_flow_insufficient_markets:{len(selected)}")
    flow_authorized_markets = sum(
        1 for row in selected if row.get("execution_role") == "FLOW_AUTHORIZED")
    authorized_execution_cell_count = sum(
        int(row.get("authorized_execution_cell_count") or 0) for row in selected)
    return {
        "schema": "polymarket_v7_maker_reward_selection_v1",
        "timestamp_ms": now_ms,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "model_sha": model_sha,
        "source": "crypto_universe_recent_flow",
        "selection_mode": "BILATERAL_AGGRESSOR_FLOW",
        "execution_cell_authority_required": True,
        "execution_authority_semantics": EXECUTION_AUTHORITY_SEMANTICS,
        "degraded": False,
        "reward_data_available": False,
        "reward_pool_count": 0,
        "reward_market_count": 0,
        "selected_count": len(selected),
        "resource_capacity_markets": resource_capacity,
        "resource_capacity": capacity_cfg,
        "universe_membership_sha256": str(universe.get("membership_sha256") or ""),
        "recent_flow_lookback_ms": lookback_ms,
        "recent_flow_latest_receive_ms": latest_receive_ms,
        "recent_flow_source": flow_source,
        "minimum_operational_markets": operational_floor,
        "observation_universe_markets": observation_universe_markets,
        "maximum_zero_flow_reserve_markets": maximum_zero_flow_reserve,
        "zero_flow_execution_fallback_enabled": (
            flow_cfg.get("zero_flow_execution_fallback_enabled") is True
        ),
        "stable_reserve_added": stable_reserve_added,
        "flow_authorized_market_count": flow_authorized_markets,
        "control_exploration_cell_count": control_cells_authorized,
        "zero_flow_execution_fallback_enabled": (
            (selection_cfg.get("recent_flow") or {}).get(
                "zero_flow_execution_fallback_enabled") is True
        ),
        "exact_cell_evidence_count": len(exact_cell_evidence or {}),
        "control_exploration_market_count": sum(
            1 for row in selected
            if row.get("control_exploration_authorized") is True),
        "authorized_execution_cell_count": authorized_execution_cell_count,
        "minimum_authorized_projected_fill_probability": (
            minimum_authorized_fill_probability),
        "unused_resource_capacity_markets": max(0, resource_capacity - len(selected)),
        "minimum_side_prints_2m": minimum_side_prints_2m,
        "maximum_last_side_age_ms": maximum_last_side_age_ms,
        "markets": selected,
        "note": "PAPER maker observes the configured universe but execution is fail-closed to selector-authorized token/action/side cells. Fresh opposite flow may authorize multiple economic placements. Sub-threshold controls are causal, per market-side, bounded by the exploration market cap, and expose both positive JOIN and IMPROVE1 arms when available. Zero-flow execution fallback is controlled by an explicit frozen policy switch; when disabled, quiet markets remain observation-only. The runtime still requires positive point EV. Rewards remain zero unless verified.",
    }


def _validated_config(config_path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], int]:
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    if cfg.get("paper_only") is not True or cfg.get("authenticated_execution") is not False or cfg.get("real_order_submission") is not False:
        raise ValueError("maker_selector_requires_paper_auth_disabled")
    selection_cfg = cfg.get("market_selection") or {}
    capacity_cfg = selection_cfg.get("resource_capacity") if isinstance(selection_cfg.get("resource_capacity"), dict) else {}
    resource_capacity = int(capacity_cfg.get("shard_count_budget", 0)) * int(capacity_cfg.get("markets_per_shard", 0))
    configured_capacity = int(selection_cfg.get("max_active_markets", 0))
    if resource_capacity <= 0 or configured_capacity != resource_capacity:
        raise ValueError("maker market capacity must equal declared shard resource capacity")
    flow_cfg = selection_cfg.get("recent_flow")
    if (
        not isinstance(flow_cfg, dict)
        or flow_cfg.get("zero_flow_execution_fallback_enabled") not in {True, False}
    ):
        raise ValueError("maker zero-flow execution fallback policy invalid")
    anchor = selection_cfg.get("settlement_anchor")
    if (
        not isinstance(anchor, dict)
        or anchor.get("enabled") is not True
        or anchor.get("execution_authority_enabled") not in {True, False}
        or int(anchor.get("maximum_slots") or 0) != 1
        or anchor.get("paper_exploration_only") is not True
        or int(anchor.get("maximum_control_markets") or 0) < 1
        or int(anchor.get("maximum_control_markets") or 0) > resource_capacity
        or str(anchor.get("required_slug_prefix") or "") != "btc-updown-5m-"
        or str(anchor.get("clob_url") or "").rstrip("/") != "https://clob.polymarket.com"
        or not 0.1 <= finite(anchor.get("request_timeout_seconds"), 0.0) <= 10.0
        or not 0 <= int(anchor.get("maximum_clock_skew_ms") or -1) <= 1_000
        or not 0.0 < finite(anchor.get("minimum_point_edge_per_share"), 0.0) < 0.25
        or anchor.get("fill_probability_source") != "EXECUTION_MODEL_GLOBAL_POSTERIOR"
        or anchor.get("evict_observation_only_market") is not True
        or anchor.get("research_only") is not True
        or anchor.get("real_money_authority") is not False
    ):
        raise ValueError("maker settlement anchor config invalid")
    return cfg, selection_cfg, capacity_cfg, resource_capacity


def _validated_observation_budget(allocation_path: Path) -> float:
    allocation = json.loads(allocation_path.read_text(encoding="utf-8"))
    v7 = allocation.get("v7") if isinstance(allocation.get("v7"), dict) else {}
    scope = allocation.get("capital_scope") if isinstance(allocation.get("capital_scope"), dict) else {}
    if (
        allocation.get("paper_only") is not True
        or v7.get("authenticated_execution") is not False
        or v7.get("real_order_submission") is not False
        or scope.get("schema") != "polymarket_v7_capital_scope_v3"
        or scope.get("allocator_owner") != "V7_CANONICAL_ALLOCATOR"
        or scope.get("view_id") != "micro_maker"
        or scope.get("scope_class") != "COMPONENT_OBSERVATION"
        or scope.get("engine_id") != "CRYPTO_SETTLEMENT_ENGINE"
        or scope.get("component") != "professional_maker"
        or scope.get("observation_budget_is_capital") is not False
        or scope.get("independent_capital_authority") is not False
        or scope.get("independent_oms_authority") is not False
        or scope.get("independent_ledger_authority") is not False
        or scope.get("double_counting_forbidden") is not True
    ):
        raise ValueError("maker_observation_allocation_contract_invalid")
    execution_budget = finite(scope.get("execution_budget"), -1.0)
    observation_budget = finite(scope.get("observation_budget"), -1.0)
    starting_capital = finite(allocation.get("starting_capital"), -1.0)
    if execution_budget != 0.0 or starting_capital != 0.0 or observation_budget <= 0.0:
        raise ValueError("maker_observation_budget_mismatch")
    return observation_budget


def _fallback_snapshot(
    universe_path: Path,
    selection_cfg: dict[str, Any],
    capacity_cfg: dict[str, Any],
    resource_capacity: int,
    *,
    model_sha: str,
    primary_error: str,
    now_ms: int,
    maximum_markets: int | None = None,
    exact_cell_evidence: dict[
        tuple[str, str, str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    universe = json.loads(universe_path.read_text(encoding="utf-8"))
    if (
        universe.get("schema") != UNIVERSE_SCHEMA
        or universe.get("paper_only") is not True
        or universe.get("authenticated_execution") is not False
        or universe.get("real_order_submission") is not False
        or universe.get("execution_authority") is not False
        or universe.get("discovery_exhaustive") is not True
        or universe.get("pagination_loop_guard_hit") is not False
        or universe.get("model_sha") != model_sha
    ):
        raise ValueError("maker_fallback_universe_contract_invalid")
    maximum_age_ms = int(float(selection_cfg.get("fallback_universe_max_age_seconds", 180.0)) * 1000.0)
    age_ms = now_ms - int(universe.get("timestamp_ms") or 0)
    if age_ms < -5_000 or age_ms > maximum_age_ms:
        raise ValueError(f"maker_fallback_universe_stale:{age_ms}")
    minimum_volume = float(selection_cfg.get("min_volume_24h", 100.0))
    minimum_liquidity = float(selection_cfg.get("min_liquidity", 25.0))
    minimum_mid = float(selection_cfg.get("min_mid", 0.02))
    maximum_mid = float(selection_cfg.get("max_mid", 0.98))
    maximum_spread = max(0.0, float(selection_cfg.get("fallback_maximum_spread", 0.10)))
    recent_flow_cfg = selection_cfg.get("recent_flow") \
        if isinstance(selection_cfg.get("recent_flow"), dict) else {}
    minimum_tte_ms = max(0, int(float(
        recent_flow_cfg.get("minimum_time_to_end_seconds", 900.0)) * 1000.0))
    weights = selection_cfg.get("fallback_score_weights") if isinstance(selection_cfg.get("fallback_score_weights"), dict) else {}
    candidates: list[dict[str, Any]] = []
    for raw in universe.get("markets") if isinstance(universe.get("markets"), list) else []:
        if not isinstance(raw, dict):
            continue
        tokens = raw.get("clob_token_ids") if isinstance(raw.get("clob_token_ids"), list) else []
        events = raw.get("event_ids") if isinstance(raw.get("event_ids"), list) else []
        liquidity = max(0.0, finite(raw.get("liquidity")))
        volume_24h = max(0.0, finite(raw.get("volume_24h")))
        midpoint = finite(raw.get("midpoint"), -1.0)
        spread = max(0.0, finite(raw.get("spread")))
        end_ms = _iso_timestamp_ms(raw.get("end_date"))
        if (
            raw.get("active") is not True
            or raw.get("closed") is True
            or raw.get("accepting_orders") is not True
            or len(tokens) < 2
            or liquidity < minimum_liquidity
            or volume_24h < minimum_volume
            or midpoint < minimum_mid
            or midpoint > maximum_mid
            or spread <= 0.0
            or spread > maximum_spread
            or (minimum_tte_ms > 0
                and (end_ms <= 0 or end_ms - now_ms < minimum_tte_ms))
        ):
            continue
        condition_id = str(raw.get("condition_id") or "")
        market_id = str(raw.get("market_id") or "")
        yes_token, no_token = str(tokens[0] or ""), str(tokens[1] or "")
        if not condition_id or not market_id or not yes_token or not no_token or yes_token == no_token:
            continue
        flow_to_depth = volume_24h / max(liquidity, minimum_liquidity)
        mid_balance = max(0.0, 1.0 - abs(midpoint - 0.5) / 0.5)
        score = (
            finite(weights.get("log_volume_24h"), 1.0) * math.log1p(volume_24h)
            + finite(weights.get("log_flow_to_depth"), 1.0) * math.log1p(flow_to_depth)
            + finite(weights.get("mid_balance"), 0.25) * mid_balance
            + finite(weights.get("log_spread_cents"), 0.1) * math.log1p(spread * 100.0)
        )
        candidates.append({
            "condition_id": condition_id,
            "market_id": market_id,
            "event_id": str(events[0] if events else ""),
            "slug": str(raw.get("slug") or ""),
            "question": str(raw.get("question") or ""),
            "yes_token": yes_token,
            "no_token": no_token,
            "volume_24h": volume_24h,
            "liquidity": liquidity,
            "midpoint": midpoint,
            "spread": spread,
            "flow_to_depth_24h": flow_to_depth,
            "market_competitiveness": 0.0,
            "rewards_max_spread_cents": 0.0,
            "rewards_min_size": 0.0,
            "native_daily_rate": 0.0,
            "sponsored_daily_rate": 0.0,
            "total_daily_rate": 0.0,
            "reward_intensity": 0.0,
            "selection_score": score,
        })
    candidates.sort(key=lambda row: (
        -finite(row.get("selection_score")),
        -finite(row.get("volume_24h")),
        finite(row.get("liquidity")),
        str(row.get("market_id") or ""),
    ))
    # This path has no causal aggressor-flow authority.  It exists only so a
    # fresh process can collect one tightly-budgeted positive-point-EV
    # exploration cell while the canonical WS flow plane warms up.  Treating
    # the shard capacity as a target here used to seed and quote 40 unrelated
    # markets, reproducing the exact no-fill dilution that the flow selector is
    # meant to remove.
    cold_start_maximum = min(
        resource_capacity,
        max(1, int(
            selection_cfg.get("cold_start_maximum_markets", 1)
            if maximum_markets is None else maximum_markets
        )),
    )
    selected: list[dict[str, Any]] = []
    selected_events: set[str] = set()
    for row in candidates:
        event_key = str(row.get("event_id") or f"market:{row.get('market_id')}")
        if event_key in selected_events:
            continue
        selected_events.add(event_key)
        selected.append({
            **row,
            "side_mode": "STABLE_SPREAD_EXPLORATION",
            "recent_prints": 0,
            "recent_unique_transactions": 0,
            "recent_share_volume": 0.0,
            "recent_notional_usd": 0.0,
            "recent_flow_to_liquidity": 0.0,
            "recent_last_trade_age_ms": -1,
            "recent_buy_prints_5s": 0,
            "recent_buy_prints_30s": 0,
            "recent_buy_prints_2m": 0,
            "recent_buy_prints_10m": 0,
            "recent_buy_share_volume_10m": 0.0,
            "recent_buy_notional_usd_10m": 0.0,
            "recent_last_buy_age_ms": -1,
            "recent_sell_prints_5s": 0,
            "recent_sell_prints_30s": 0,
            "recent_sell_prints_2m": 0,
            "recent_sell_prints_10m": 0,
            "recent_sell_share_volume_10m": 0.0,
            "recent_sell_notional_usd_10m": 0.0,
            "recent_last_sell_age_ms": -1,
            "quote_opportunities": [],
            "execution_role": "WARM_FALLBACK_OBSERVATION",
            "control_exploration_authorized": False,
            "authorized_execution_cells": [],
            "authorized_execution_cell_count": 0,
        })
        if len(selected) >= cold_start_maximum:
            break
    if not selected:
        raise ValueError("maker_fallback_universe_has_no_eligible_markets")
    control_cells_authorized = (
        _authorize_one_control_cell(
            selected, exact_cell_evidence=exact_cell_evidence
        )
        if ((selection_cfg.get("recent_flow") or {}).get(
            "zero_flow_execution_fallback_enabled") is True)
        else 0
    )
    _annotate_inventory_seed_authority(selected)
    return {
        "schema": "polymarket_v7_maker_reward_selection_v1",
        "timestamp_ms": now_ms,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "model_sha": model_sha,
        "source": "crypto_universe_fallback",
        "selection_mode": "FLOW_FILLABILITY_FALLBACK",
        "execution_cell_authority_required": True,
        "execution_authority_semantics": EXECUTION_AUTHORITY_SEMANTICS,
        "degraded": True,
        "reward_data_available": False,
        "primary_error": primary_error,
        "reward_pool_count": 0,
        "reward_market_count": 0,
        "selected_count": len(selected),
        "resource_capacity_markets": resource_capacity,
        "cold_start_maximum_markets": cold_start_maximum,
        "authorized_execution_cell_count": control_cells_authorized,
        "control_exploration_cell_count": control_cells_authorized,
        "exact_cell_evidence_count": len(exact_cell_evidence or {}),
        "unused_resource_capacity_markets": max(0, resource_capacity - len(selected)),
        "resource_capacity": capacity_cfg,
        "universe_membership_sha256": str(universe.get("membership_sha256") or ""),
        "markets": selected,
        "note": "Causal aggressor flow is unavailable; PAPER maker keeps the warm observation cohort but grants execution to exactly one explicit JOIN/YES/BUY cold-start control cell. Reward assumptions remain zero.",
    }


def build_snapshot(
    config_path: Path,
    *,
    fallback_universe_path: Path | None = None,
    trade_tape_path: Path | None = None,
    book_tape_path: Path | None = None,
    model_sha: str = "",
    now_ms: int | None = None,
    sleeve_capital: float | None = None,
    allocation_path: Path | None = None,
    execution_model_path: Path | None = None,
) -> dict[str, Any]:
    cfg, selection_cfg, capacity_cfg, resource_capacity = _validated_config(config_path)
    if len(model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in model_sha.lower()):
        raise ValueError("model_sha must be a 40-character hexadecimal SHA")
    model_sha = model_sha.lower()
    if fallback_universe_path is None:
        raise ValueError("maker_crypto_universe_required")
    exact_cell_evidence = _load_exact_cell_evidence(
        execution_model_path, model_sha=model_sha)
    if sleeve_capital is not None and allocation_path is not None:
        raise ValueError("maker_observation_budget_source_ambiguous")
    expected_budget = finite(selection_cfg.get("observation_budget_usd"), 0.0)
    if allocation_path is not None:
        sleeve_capital = _validated_observation_budget(allocation_path)
    elif sleeve_capital is None:
        sleeve_capital = expected_budget
    if expected_budget <= 0.0 or abs(float(sleeve_capital) - expected_budget) > 1e-9:
        raise ValueError("maker_observation_budget_policy_mismatch")

    current_ms = time.time_ns() // 1_000_000 if now_ms is None else int(now_ms)
    flow_cfg = selection_cfg.get("recent_flow") if isinstance(selection_cfg.get("recent_flow"), dict) else {}
    flow_error = "maker_recent_flow_disabled"
    if flow_cfg.get("enabled") is True and trade_tape_path is not None:
        flow_wait_seconds = max(0.0, float(flow_cfg.get("initial_wait_seconds", 0.0)))
        flow_deadline = time.monotonic() + flow_wait_seconds
        flow_error = "maker_crypto_trade_flow_unavailable"
        while True:
            try:
                return _recent_flow_snapshot(
                    fallback_universe_path, selection_cfg, capacity_cfg,
                    resource_capacity, model_sha=model_sha, now_ms=current_ms,
                    trade_tape_path=trade_tape_path,
                    book_tape_path=book_tape_path,
                    exact_cell_evidence=exact_cell_evidence,
                )
            except Exception as error:
                flow_error = f"{type(error).__name__}:{error}"[:500]
                if time.monotonic() >= flow_deadline:
                    break
                time.sleep(min(1.0, max(0.0, flow_deadline - time.monotonic())))
    elif flow_cfg.get("enabled") is True:
        flow_error = "maker_crypto_trade_tape_missing"

    # Cold start is allowed only inside the same exact-SHA crypto universe and
    # remains observation-only unless frozen exact-cell evidence authorizes it.
    return _fallback_snapshot(
        fallback_universe_path, selection_cfg, capacity_cfg, resource_capacity,
        model_sha=model_sha, primary_error=flow_error, now_ms=current_ms,
        exact_cell_evidence=exact_cell_evidence,
    )


def _validated_pinned_selection(path: Path, *, model_sha: str) -> dict[str, Any]:
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    markets = snapshot.get("markets") if isinstance(snapshot.get("markets"), list) else []
    if (
        snapshot.get("schema") != "polymarket_v7_maker_reward_selection_v1"
        or snapshot.get("paper_only") is not True
        or snapshot.get("authenticated_execution") is not False
        or snapshot.get("real_order_submission") is not False
        or snapshot.get("model_sha") != model_sha
        or snapshot.get("execution_cell_authority_required") is not True
        or snapshot.get("execution_authority_semantics") != EXECUTION_AUTHORITY_SEMANTICS
        or snapshot.get("source") not in {
            "crypto_universe_fallback", "crypto_universe_recent_flow",
        }
        or not markets
        or int(snapshot.get("selected_count") or 0) != len(markets)
        or len(markets) > int(snapshot.get("resource_capacity_markets") or 0)
        or any(
            not all(str(row.get(key) or "") for key in (
                "condition_id", "market_id", "yes_token", "no_token",
            ))
            or str(row.get("yes_token")) == str(row.get("no_token"))
            for row in markets
            if isinstance(row, dict)
        )
        or any(not isinstance(row, dict) for row in markets)
    ):
        raise ValueError("maker_pinned_runtime_selection_invalid")
    return snapshot


def publish_runtime_selection(
    candidate: dict[str, Any],
    output_path: Path,
    *,
    pin_runtime_selection: bool,
    candidate_output_path: Path | None = None,
) -> tuple[dict[str, Any], bool]:
    if candidate_output_path is not None:
        atomic_json(candidate_output_path, candidate)
    pinned = pin_runtime_selection and output_path.is_file()
    if pinned:
        runtime = _validated_pinned_selection(
            output_path, model_sha=str(candidate.get("model_sha") or "")
        )
    else:
        atomic_json(output_path, candidate)
        runtime = candidate
    return runtime, pinned


def selector_status(
    snapshot: dict[str, Any],
    *,
    candidate_snapshot: dict[str, Any] | None = None,
    runtime_selection_pinned: bool = False,
) -> dict[str, Any]:
    candidate = snapshot if candidate_snapshot is None else candidate_snapshot
    runtime_membership = selection_membership_sha256(snapshot)
    candidate_membership = selection_membership_sha256(candidate)
    candidate_markets = candidate.get("markets") if isinstance(candidate.get("markets"), list) else []
    candidate_flow_eligible = (
        candidate.get("source") == "crypto_universe_recent_flow"
        and candidate.get("degraded") is not True
    )
    candidate_rotation_suppressed = bool(
        runtime_selection_pinned
        and candidate.get("degraded") is True
        and runtime_membership != candidate_membership
    )
    candidate_last_sell_ages = [
        max(0.0, finite(row.get("recent_last_sell_age_ms")) / 1_000.0)
        for row in candidate_markets
        if isinstance(row, dict) and finite(row.get("recent_last_sell_age_ms"), -1.0) >= 0.0
    ]
    return {
        "schema": SELECTOR_STATUS_SCHEMA,
        "timestamp_ms": candidate.get("timestamp_ms"),
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "model_sha": snapshot.get("model_sha"),
        "state": (
            "OPERATIONAL_FALLBACK" if candidate.get("degraded")
            else "OPERATIONAL_BILATERAL_FLOW"
            if candidate.get("source") == "crypto_universe_recent_flow"
            else "OPERATIONAL_CRYPTO_SELECTION"
        ),
        "ready": True,
        "degraded": candidate.get("degraded") is True,
        "source": candidate.get("source"),
        "selected_count": snapshot.get("selected_count", 0),
        "reward_pool_count": candidate.get("reward_pool_count", 0),
        "reward_market_count": candidate.get("reward_market_count", 0),
        "primary_error": candidate.get("primary_error", ""),
        "runtime_selection_pinned": runtime_selection_pinned,
        "runtime_membership_sha256": runtime_membership,
        "candidate_membership_sha256": candidate_membership,
        "candidate_rotation_pending": (
            not candidate_rotation_suppressed
            and runtime_membership != candidate_membership
        ),
        "candidate_rotation_suppressed_no_fresh_flow": candidate_rotation_suppressed,
        "candidate_source": candidate.get("source"),
        "candidate_degraded": candidate.get("degraded") is True,
        "candidate_selected_count": candidate.get("selected_count", 0),
        "candidate_fresh_flow_eligible": candidate_flow_eligible,
        "candidate_selected_bilateral": sum(
            1 for row in candidate_markets
            if isinstance(row, dict) and row.get("side_mode") == "BILATERAL"
        ),
        "candidate_selected_inventory_backed_ask": sum(
            1 for row in candidate_markets
            if isinstance(row, dict) and row.get("side_mode") == "INVENTORY_BACKED_ASK"
        ),
        "candidate_selected_collateral_backed_bid": sum(
            1 for row in candidate_markets
            if isinstance(row, dict) and row.get("side_mode") == "COLLATERAL_BACKED_BID"
        ),
        "candidate_selected_stable_spread_exploration": sum(
            1 for row in candidate_markets
            if isinstance(row, dict) and row.get("side_mode") == "STABLE_SPREAD_EXPLORATION"
        ),
        "candidate_selected_with_buy_flow_30s": sum(
            1 for row in candidate_markets
            if isinstance(row, dict) and int(row.get("recent_buy_prints_30s") or 0) > 0
        ),
        "candidate_selected_with_buy_flow_2m": sum(
            1 for row in candidate_markets
            if isinstance(row, dict) and int(row.get("recent_buy_prints_2m") or 0) > 0
        ),
        "candidate_selected_with_sell_flow_30s": sum(
            1 for row in candidate_markets
            if isinstance(row, dict) and int(row.get("recent_sell_prints_30s") or 0) > 0
        ),
        "candidate_selected_with_sell_flow_2m": sum(
            1 for row in candidate_markets
            if isinstance(row, dict) and int(row.get("recent_sell_prints_2m") or 0) > 0
        ),
        "candidate_max_last_sell_age_seconds": (
            max(candidate_last_sell_ages) if candidate_last_sell_ages else -1.0
        ),
        "settlement_anchor_state": candidate.get("settlement_anchor_state", "DISABLED"),
        "settlement_anchor_authorized": candidate.get("settlement_anchor_authorized") is True,
        "settlement_anchor_preserved_flow_authority": (
            candidate.get("settlement_anchor_preserved_flow_authority") is True
        ),
        "settlement_anchor_observed_flow_authority": (
            candidate.get("settlement_anchor_observed_flow_authority") is True
        ),
        "settlement_anchor_market_id": str(candidate.get("settlement_anchor_market_id") or ""),
        "settlement_anchor_fill_probability_source": str(
            candidate.get("settlement_anchor_fill_probability_source") or ""
        ),
        "settlement_anchor_identity_source": str(
            candidate.get("settlement_anchor_identity_source") or ""
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/v7_professional_market_maker.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate-output", type=Path)
    parser.add_argument("--pin-runtime-selection", action="store_true")
    parser.add_argument("--status", type=Path)
    parser.add_argument("--fallback-universe", type=Path)
    parser.add_argument("--trade-tape", type=Path)
    parser.add_argument("--book-tape", type=Path)
    parser.add_argument("--allocation", type=Path)
    parser.add_argument("--execution-model", type=Path)
    parser.add_argument("--settlement-fair-status", type=Path)
    parser.add_argument("--anchor-flow", type=Path)
    parser.add_argument("--model-sha", default="")
    parser.add_argument("--event-log",type=Path)
    args = parser.parse_args()
    snapshot = build_snapshot(
        args.config,
        fallback_universe_path=args.fallback_universe,
        trade_tape_path=args.trade_tape,
        book_tape_path=args.book_tape,
        allocation_path=args.allocation,
        execution_model_path=args.execution_model,
        model_sha=args.model_sha,
    )
    if args.settlement_fair_status is not None:
        _, selection_cfg, _, _ = _validated_config(args.config)
        snapshot = _inject_settlement_anchor(
            snapshot,
            fair_status_path=args.settlement_fair_status,
            universe_path=args.fallback_universe,
            execution_model_path=args.execution_model,
            anchor_flow_path=args.anchor_flow,
            selection_cfg=selection_cfg,
            model_sha=args.model_sha.lower(),
            now_ms=int(snapshot.get("timestamp_ms") or time.time_ns() // 1_000_000),
        )
    runtime_snapshot, pinned = publish_runtime_selection(
        snapshot,
        args.output,
        pin_runtime_selection=args.pin_runtime_selection,
        candidate_output_path=args.candidate_output,
    )
    if args.status is not None:
        atomic_json(args.status, selector_status(
            runtime_snapshot,
            candidate_snapshot=snapshot,
            runtime_selection_pinned=pinned,
        ))
    if args.event_log:
        from v7_compressed_journal import CompressedJournal
        with CompressedJournal(args.event_log) as journal:journal.append(runtime_snapshot)
    else:print(json.dumps(runtime_snapshot, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
