#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import v7_cross_sectional_rank_core as core

INTENT_FIELDS = [
    "bundle_id", "strategy", "event_id", "created_ts", "mode", "expected_edge",
    "max_notional", "market_id", "side", "weight", "limit_price",
    "execution_deadline_ts", "hold_deadline_ts",
]
HISTORY_WINDOW_SECONDS = 7 * 24 * 60 * 60
HISTORY_BATCH_SIZE = 20
HISTORY_SINGLE_FALLBACK_BUDGET_PER_WINDOW = 40


def finite(value: Any, default: float = math.nan) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def parse_array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            out = json.loads(value)
        except json.JSONDecodeError:
            return []
        return out if isinstance(out, list) else []
    return []


def request_json(url: str, payload: Any | None = None, timeout: int = 20) -> Any:
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "User-Agent": "polymarket-v7-xsec-research/2",
        "Accept": "application/json",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def http_error_detail(exc: BaseException) -> str:
    if not isinstance(exc, urllib.error.HTTPError):
        return type(exc).__name__
    detail = ""
    try:
        detail = exc.read(240).decode("utf-8", errors="replace").strip().replace("\n", " ")
    except Exception:
        pass
    return f"HTTP{exc.code}" + (f":{detail}" if detail else "")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def atomic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=INTENT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


@dataclass(frozen=True)
class Market:
    market_id: str
    event_id: str
    group: str
    yes_token: str
    no_token: str
    liquidity: float
    raw: dict[str, Any]


def market_group(raw: dict[str, Any]) -> str:
    direct = str(raw.get("category") or "").strip().lower()
    if direct:
        return direct
    events = raw.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        category = str(events[0].get("category") or "").strip().lower()
        if category:
            return category
    return "global"


def parse_market(raw: dict[str, Any]) -> Market | None:
    tokens = [str(x) for x in parse_array(raw.get("clobTokenIds"))]
    outcomes = [str(x).lower() for x in parse_array(raw.get("outcomes"))]
    if len(tokens) < 2:
        return None
    yi, ni = 0, 1
    for index, outcome in enumerate(outcomes[: len(tokens)]):
        if outcome == "yes":
            yi = index
        elif outcome == "no":
            ni = index
    market_id = str(raw.get("id") or "").strip()
    if not market_id:
        return None
    event_id = str(raw.get("eventId") or "").strip()
    events = raw.get("events")
    if not event_id and isinstance(events, list) and events and isinstance(events[0], dict):
        event_id = str(events[0].get("id") or "").strip()
    condition = str(raw.get("conditionId") or "").strip()
    return Market(
        market_id=market_id,
        event_id=event_id or condition or market_id,
        group=market_group(raw),
        yes_token=tokens[yi],
        no_token=tokens[ni],
        liquidity=max(0.0, finite(raw.get("liquidityNum"), 0.0)),
        raw=dict(raw),
    )


def discover_markets(gamma: str, limit: int, min_liquidity: float) -> list[Market]:
    out: list[Market] = []
    offset = 0
    while len(out) < limit and offset < 5000:
        query = urllib.parse.urlencode(
            {
                "active": "true",
                "closed": "false",
                "limit": 100,
                "offset": offset,
                "order": "liquidityNum",
                "ascending": "false",
            }
        )
        payload = request_json(gamma.rstrip("/") + "/markets?" + query)
        batch = payload if isinstance(payload, list) else payload.get("markets", []) if isinstance(payload, dict) else []
        if not batch:
            break
        for raw in batch:
            market = parse_market(raw) if isinstance(raw, dict) else None
            if market is not None and market.liquidity >= min_liquidity:
                out.append(market)
                if len(out) >= limit:
                    break
        if len(batch) < 100:
            break
        offset += 100
    return out


def parse_history(rows: list[Any], fidelity_minutes: int) -> dict[int, float]:
    bucket = fidelity_minutes * 60
    out: dict[int, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = int(finite(row.get("t"), 0.0))
        price = finite(row.get("p"))
        if ts > 0 and math.isfinite(price) and 0.0 < price < 1.0:
            out[(ts // bucket) * bucket] = price
    return out


def _merge_history(
    histories: dict[str, dict[int, float]],
    market_id: str,
    rows: list[Any],
    fidelity_minutes: int,
) -> int:
    parsed = parse_history(rows, fidelity_minutes)
    if not parsed:
        return 0
    target = histories.setdefault(market_id, {})
    before = len(target)
    target.update(parsed)
    return len(target) - before


def fetch_histories(
    clob: str,
    markets: list[Market],
    start_ts: int,
    end_ts: int,
    fidelity_minutes: int,
) -> tuple[dict[str, dict[int, float]], list[str]]:
    """Fetch absolute CLOB history in bounded windows, with per-window fallback.

    Long fine-fidelity absolute requests have previously returned HTTP 400 from the
    public history service.  The production C++ path already solved this by chunking;
    V7 research must use the same data-health contract rather than treating a long
    request failure as an empty panel.
    """
    if end_ts <= start_ts or not markets:
        return {}, []
    token_to_market = {market.yes_token: market.market_id for market in markets}
    tokens = list(token_to_market)
    histories: dict[str, dict[int, float]] = {}
    failures: list[str] = []

    window_start = start_ts
    while window_start < end_ts:
        window_end = min(end_ts, window_start + HISTORY_WINDOW_SECONDS)
        single_budget = HISTORY_SINGLE_FALLBACK_BUDGET_PER_WINDOW
        for index in range(0, len(tokens), HISTORY_BATCH_SIZE):
            batch = tokens[index : index + HISTORY_BATCH_SIZE]
            before = {token: len(histories.get(token_to_market[token], {})) for token in batch}
            try:
                raw = request_json(
                    clob.rstrip("/") + "/batch-prices-history",
                    {
                        "markets": batch,
                        "start_ts": window_start,
                        "end_ts": window_end,
                        "fidelity": fidelity_minutes,
                    },
                )
                history = raw.get("history", {}) if isinstance(raw, dict) else {}
                if isinstance(history, dict):
                    for token, rows in history.items():
                        market_id = token_to_market.get(str(token))
                        if market_id and isinstance(rows, list):
                            _merge_history(histories, market_id, rows, fidelity_minutes)
            except Exception as exc:
                detail = http_error_detail(exc)
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                    raise RuntimeError(f"price history rate limited at {window_start}-{window_end}: {detail}") from exc
                failures.append(f"batch:{window_start}:{window_end}:{detail}")

            for token in batch:
                market_id = token_to_market[token]
                if len(histories.get(market_id, {})) > before[token] or single_budget <= 0:
                    continue
                query = urllib.parse.urlencode(
                    {
                        "market": token,
                        "startTs": window_start,
                        "endTs": window_end,
                        "fidelity": fidelity_minutes,
                    }
                )
                try:
                    raw = request_json(clob.rstrip("/") + "/prices-history?" + query)
                    rows = raw.get("history", []) if isinstance(raw, dict) else []
                    if isinstance(rows, list):
                        _merge_history(histories, market_id, rows, fidelity_minutes)
                except Exception as exc:
                    detail = http_error_detail(exc)
                    if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                        raise RuntimeError(f"price history rate limited for {market_id}: {detail}") from exc
                    failures.append(f"single:{market_id}:{window_start}:{window_end}:{detail}")
                single_budget -= 1
        window_start = window_end
    return histories, failures


def best_book(row: dict[str, Any]) -> tuple[float, float] | None:
    bids: list[float] = []
    asks: list[float] = []
    for level in row.get("bids", []):
        if isinstance(level, dict):
            price = finite(level.get("price"))
            if math.isfinite(price) and 0.0 < price < 1.0:
                bids.append(price)
    for level in row.get("asks", []):
        if isinstance(level, dict):
            price = finite(level.get("price"))
            if math.isfinite(price) and 0.0 < price < 1.0:
                asks.append(price)
    if not bids or not asks:
        return None
    bid, ask = max(bids), min(asks)
    return (bid, ask) if bid < ask else None


def fetch_books(clob: str, markets: list[Market]) -> dict[str, tuple[float, float, float, float]]:
    token_map: dict[str, tuple[str, str]] = {}
    for market in markets:
        token_map[market.yes_token] = (market.market_id, "YES")
        token_map[market.no_token] = (market.market_id, "NO")
    sides: dict[str, dict[str, tuple[float, float]]] = {}
    tokens = list(token_map)
    for index in range(0, len(tokens), 80):
        raw = request_json(clob.rstrip("/") + "/books", [{"token_id": token} for token in tokens[index : index + 80]])
        for row in raw if isinstance(raw, list) else []:
            if not isinstance(row, dict):
                continue
            identity = token_map.get(str(row.get("asset_id") or ""))
            book = best_book(row)
            if identity is None or book is None:
                continue
            market_id, side = identity
            sides.setdefault(market_id, {})[side] = book
    out: dict[str, tuple[float, float, float, float]] = {}
    for market_id, item in sides.items():
        if "YES" in item and "NO" in item:
            out[market_id] = (*item["YES"], *item["NO"])
    return out


def authoritative_fee(raw: dict[str, Any]) -> tuple[bool, float, float, bool]:
    if raw.get("feesEnabled") is False:
        return True, 0.0, 1.0, True
    schedule = raw.get("feeSchedule")
    if not isinstance(schedule, dict):
        return False, 0.0, 1.0, True
    rate = finite(schedule.get("rate"))
    exponent = finite(schedule.get("exponent"), 1.0)
    if not math.isfinite(rate) or rate < 0.0:
        return False, 0.0, 1.0, True
    return True, rate, max(0.0, exponent), bool(schedule.get("takerOnly", True))


def statistical_gate(report: dict[str, Any], cfg: dict[str, Any]) -> tuple[bool, list[str]]:
    gate = cfg["oos_gate"]
    reasons: list[str] = []
    if int(report.get("cross_sections", 0)) < int(gate["minimum_test_cross_sections"]):
        reasons.append("insufficient_test_cross_sections")
    if int(report.get("predictions", 0)) < int(gate["minimum_predictions"]):
        reasons.append("insufficient_predictions")
    if float(report.get("median_rank_ic", 0.0)) < float(gate["minimum_median_rank_ic"]):
        reasons.append("rank_ic")
    if float(report.get("positive_ic_fraction", 0.0)) < float(gate["minimum_positive_ic_fraction"]):
        reasons.append("ic_stability")
    if float(report.get("decile_monotonicity", 0.0)) < float(gate["minimum_decile_monotonicity"]):
        reasons.append("decile_monotonicity")
    if gate.get("require_positive_top_bottom_spread", True) and float(report.get("median_top_bottom_logit_spread", 0.0)) <= 0.0:
        reasons.append("top_bottom_spread")
    return not reasons, reasons


def candidate_intent(candidate: core.ExecutableCandidate, now: int) -> dict[str, Any]:
    minutes = candidate.horizon_seconds // 60
    return {
        "bundle_id": f"xsec:{minutes}m:{now}:{candidate.market_id}",
        "strategy": f"XSEC_RANK_H{minutes}M",
        "event_id": candidate.event_id,
        "created_ts": now,
        "mode": "TAKER",
        "expected_edge": f"{candidate.net_edge:.10f}",
        "max_notional": f"{candidate.max_notional:.8f}",
        "market_id": candidate.market_id,
        "side": candidate.side,
        "weight": "1.0",
        "limit_price": f"{candidate.entry_price:.8f}",
        "execution_deadline_ts": now + 120,
        "hold_deadline_ts": now + candidate.horizon_seconds,
    }


def latest_score_time(
    histories: dict[str, dict[int, float]],
    metadata: dict[str, core.MarketMeta],
    bucket_seconds: int,
    minimum: int,
) -> int:
    times = sorted({ts for series in histories.values() for ts in series}, reverse=True)
    for ts in times:
        if core.score_snapshot(histories, metadata, ts, bucket_seconds, minimum):
            return ts
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="V7 causal cross-sectional ranking research")
    parser.add_argument("--config", type=Path, default=Path("config/research_v7_cross_sectional_rank.json"))
    parser.add_argument("--gamma-url", default="https://gamma-api.polymarket.com")
    parser.add_argument("--clob-url", default="https://clob.polymarket.com")
    parser.add_argument("--market-limit", type=int, default=500)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-shadow-intents", type=Path, required=True)
    args = parser.parse_args()

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    if not cfg.get("paper_only") or not cfg.get("research_only") or cfg.get("live_intents_enabled"):
        raise SystemExit("cross-sectional ranking research config must remain paper/research-only with live intents disabled")

    now = int(time.time())
    history_cfg = cfg["history"]
    execution_cfg = cfg["execution_shadow"]
    model_cfg = cfg["model"]
    fidelity_minutes = int(history_cfg["fidelity_minutes"])
    bucket_seconds = fidelity_minutes * 60
    markets = discover_markets(args.gamma_url, args.market_limit, float(execution_cfg["minimum_liquidity_usd"]))
    start_ts = now - int(history_cfg["lookback_hours"]) * 3600
    histories, history_failures = fetch_histories(args.clob_url, markets, start_ts, now, fidelity_minutes)
    metadata = {
        market.market_id: core.MarketMeta(market.market_id, market.event_id, market.group)
        for market in markets
        if market.market_id in histories
    }

    received_ts = int(time.time())
    raw_books = fetch_books(args.clob_url, markets)
    current_bucket = (received_ts // bucket_seconds) * bucket_seconds
    for market in markets:
        raw_book = raw_books.get(market.market_id)
        if raw_book is None or market.market_id not in histories:
            continue
        yes_bid, yes_ask, _no_bid, _no_ask = raw_book
        histories[market.market_id][current_bucket] = 0.5 * (yes_bid + yes_ask)

    book_economics: dict[str, core.BookEconomics] = {}
    authoritative = 0
    for market in markets:
        raw_book = raw_books.get(market.market_id)
        if raw_book is None:
            continue
        auth, rate, exponent, taker_only = authoritative_fee(market.raw)
        authoritative += int(auth)
        yes_bid, yes_ask, no_bid, no_ask = raw_book
        book_economics[market.market_id] = core.BookEconomics(
            market_id=market.market_id,
            event_id=market.event_id,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            liquidity=market.liquidity,
            fee_rate=rate,
            fee_exponent=exponent,
            taker_only=taker_only,
            authoritative_fee=auth,
            received_ts=received_ts,
        )

    history_required = int(history_cfg["minimum_cross_section"])
    history_data_healthy = len(histories) >= history_required
    horizon_reports: list[dict[str, Any]] = []
    all_shadow_intents: list[dict[str, Any]] = []
    selected_event_side: set[tuple[str, str]] = set()
    for horizon_minutes in cfg["horizons_minutes"]:
        if int(horizon_minutes) % fidelity_minutes != 0:
            raise SystemExit(f"horizon {horizon_minutes} is not an integer multiple of fidelity {fidelity_minutes}")
        horizon_steps = int(horizon_minutes) // fidelity_minutes
        rows = core.build_training_rows(
            histories,
            metadata,
            bucket_seconds=bucket_seconds,
            horizon_steps=horizon_steps,
            min_cross_section=history_required,
            group_weight=float(history_cfg["group_neutralization_weight"]),
            min_group_size=int(history_cfg["minimum_group_size"]),
        )
        report = core.walk_forward_evaluate(
            rows,
            bucket_seconds=bucket_seconds,
            horizon_steps=horizon_steps,
            window_seconds=int(history_cfg["training_window_days"]) * 86400,
            embargo_steps=int(history_cfg["purge_embargo_buckets"]),
            ridge=float(model_cfg["ridge_penalty"]),
            half_life_seconds=int(history_cfg["recency_half_life_days"]) * 86400,
            min_train_rows=int(model_cfg["minimum_training_rows"]),
            min_train_cross_sections=int(model_cfg["minimum_training_cross_sections"]),
        )
        gate_ok, gate_reasons = statistical_gate(report, cfg)
        if not history_data_healthy and "price_history_data_health" not in gate_reasons:
            gate_reasons.insert(0, "price_history_data_health")
            gate_ok = False
        score_ts = latest_score_time(histories, metadata, bucket_seconds, history_required)
        selected: list[core.ExecutableCandidate] = []
        fit = None
        if gate_ok and score_ts > 0 and received_ts - score_ts <= 2 * bucket_seconds:
            fit = core.fit_ridge(
                rows,
                asof_ts=score_ts,
                window_seconds=int(history_cfg["training_window_days"]) * 86400,
                embargo_seconds=int(history_cfg["purge_embargo_buckets"]) * bucket_seconds,
                ridge=float(model_cfg["ridge_penalty"]),
                half_life_seconds=int(history_cfg["recency_half_life_days"]) * 86400,
                min_rows=int(model_cfg["minimum_training_rows"]),
                min_cross_sections=int(model_cfg["minimum_training_cross_sections"]),
            )
            snapshot = core.score_snapshot(histories, metadata, score_ts, bucket_seconds, history_required)
            if fit is not None and snapshot:
                scored = core.apply_fit(snapshot, fit, score_ts)
                selected = core.select_candidates(
                    scored,
                    book_economics,
                    horizon_seconds=int(horizon_minutes) * 60,
                    now=received_ts,
                    min_net_edge=float(execution_cfg["minimum_net_edge"]),
                    max_positions_per_side=int(execution_cfg["maximum_positions_per_side"]),
                    max_trade_usd=float(execution_cfg["maximum_trade_usd"]),
                    sleeve_budget_usd=float(execution_cfg["shadow_sleeve_budget_usd"]),
                    min_liquidity=float(execution_cfg["minimum_liquidity_usd"]),
                    max_spread=float(execution_cfg["maximum_spread"]),
                    slippage_bps=float(execution_cfg["slippage_bps_round_trip_leg"]),
                    capital_cost_bps_per_hour=float(execution_cfg["capital_cost_bps_per_hour"]),
                    adverse_penalty_bps=float(execution_cfg["adverse_markout_penalty_bps"]),
                    max_book_age_seconds=int(execution_cfg["maximum_book_age_seconds"]),
                )
        for candidate in selected:
            key = (candidate.event_id, candidate.side)
            if key in selected_event_side:
                continue
            selected_event_side.add(key)
            all_shadow_intents.append(candidate_intent(candidate, received_ts))
        horizon_reports.append(
            {
                "horizon_minutes": int(horizon_minutes),
                "training_rows": len(rows),
                "score_timestamp": score_ts,
                "statistical_gate": gate_ok,
                "gate_reasons": gate_reasons,
                "oos": report,
                "fit": None if fit is None else asdict(fit),
                "shadow_candidates": [asdict(candidate) for candidate in selected],
            }
        )

    atomic_csv(args.output_shadow_intents, all_shadow_intents)
    universe_mode = str(cfg.get("universe_history", {}).get("current_research_mode") or "current_active_survivors")
    report = {
        "timestamp": received_ts,
        "paper_only": True,
        "research_only": True,
        "live_intents_enabled": False,
        "submitted_orders": 0,
        "universe_backtest_mode": universe_mode,
        "survivorship_safe": False,
        "market_count": len(markets),
        "history_market_count": len(histories),
        "history_required_market_count": history_required,
        "history_data_healthy": history_data_healthy,
        "history_window_days": HISTORY_WINDOW_SECONDS // 86400,
        "book_market_count": len(raw_books),
        "authoritative_fee_book_count": authoritative,
        "history_failures": history_failures[:50],
        "feature_names": list(core.FEATURE_NAMES),
        "target": cfg["target"],
        "horizons": horizon_reports,
        "shadow_intent_rows": len(all_shadow_intents),
        "economic_pnl_validated": False,
        "promotion_ready": False,
        "promotion_blockers": [
            "point_in_time_or_forward_universe_not_yet_attached",
            "shared_execution_ledger_cost_stressed_pnl_not_yet_attached",
            "research_branch_cannot_mutate_live_champion",
            "live_intents_disabled_by_config",
        ] + ([] if history_data_healthy else ["price_history_data_health_unhealthy"]),
    }
    atomic_json(args.output_json, report)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())