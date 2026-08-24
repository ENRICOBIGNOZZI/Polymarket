#!/usr/bin/env python3
"""Research-only event-time diagnostics for the V5 micro sleeve.

The active V5 `micro` expert is a contemporaneous fair-probability proxy. This
module also supports a distinct causal seconds-to-minutes experiment: replay a
read-only WebSocket capture, build order-book features known at event receipt,
label future midpoint markout, and compare a microprice-only baseline with a
regularized imbalance/OFI challenger on the same chronological rows.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

FEATURES = (
    "micro_displacement",
    "imbalance_l1",
    "imbalance_l3",
    "imbalance_l5",
    "ofi_l1",
    "spread",
    "feed_latency_ms",
)
REQUIRED_COLUMNS = {
    "exchange_ts_ms", "market_id", "mid", "spread", "microprice",
    "imbalance_l1", "imbalance_l3", "imbalance_l5", "ofi_l1",
    "feed_latency_ms",
}
FEATURE_COLUMNS = (
    "exchange_ts_ms", "received_ts_ms", "market_id", "mid", "spread",
    "microprice", "bid_depth_l1", "ask_depth_l1", "bid_depth_l3",
    "ask_depth_l3", "bid_depth_l5", "ask_depth_l5", "imbalance_l1",
    "imbalance_l3", "imbalance_l5", "ofi_l1", "feed_latency_ms",
)


def _finite(value: object) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _timestamp_ms(value: object) -> int:
    x = _finite(value) or 0.0
    ts = int(x)
    return ts * 1000 if 0 < ts < 100_000_000_000 else ts


def microprice(best_bid: float, best_ask: float, bid_depth: float, ask_depth: float) -> float:
    if not all(math.isfinite(x) for x in (best_bid, best_ask, bid_depth, ask_depth)):
        raise ValueError("microprice inputs must be finite")
    if best_ask < best_bid:
        raise ValueError("crossed book")
    bid_depth, ask_depth = max(0.0, bid_depth), max(0.0, ask_depth)
    if bid_depth + ask_depth <= 1e-12:
        return 0.5 * (best_bid + best_ask)
    return min(best_ask, max(best_bid, (best_ask * bid_depth + best_bid * ask_depth) / (bid_depth + ask_depth)))


def incumbent_micro_fair(
    yes_micro: float, no_micro: float, yes_depth: float, no_depth: float,
    yes_spread: float, no_spread: float,
) -> float:
    wy = math.sqrt(max(0.0, yes_depth)) / (1.0 + 20.0 * max(0.0, yes_spread))
    wn = math.sqrt(max(0.0, no_depth)) / (1.0 + 20.0 * max(0.0, no_spread))
    if wy + wn <= 1e-12:
        return 0.5
    return min(0.999, max(0.001, (wy * yes_micro + wn * (1.0 - no_micro)) / (wy + wn)))


def mirrored_binary_invariant(
    yes_bid: float = 0.48, yes_ask: float = 0.50,
    yes_bid_depth: float = 100.0, yes_ask_depth: float = 50.0,
) -> dict[str, float | bool]:
    no_bid, no_ask = 1.0 - yes_ask, 1.0 - yes_bid
    no_bid_depth, no_ask_depth = yes_ask_depth, yes_bid_depth
    y = microprice(yes_bid, yes_ask, yes_bid_depth, yes_ask_depth)
    n = microprice(no_bid, no_ask, no_bid_depth, no_ask_depth)
    q = incumbent_micro_fair(
        y, n, yes_bid_depth + yes_ask_depth, no_bid_depth + no_ask_depth,
        yes_ask - yes_bid, no_ask - no_bid,
    )
    yes_edge, no_edge = q - yes_ask, (1.0 - q) - no_ask
    return {
        "yes_microprice": y,
        "no_microprice": n,
        "complement_error": abs(y - (1.0 - n)),
        "incumbent_q_yes": q,
        "yes_taker_gross_edge": yes_edge,
        "no_taker_gross_edge": no_edge,
        "no_positive_taker_edge": yes_edge <= 1e-12 and no_edge <= 1e-12,
    }


def _levels(value: object) -> dict[float, float]:
    out: dict[float, float] = {}
    if not isinstance(value, list):
        return out
    for item in value:
        if not isinstance(item, dict):
            continue
        p, q = _finite(item.get("price")), _finite(item.get("size"))
        if p is not None and q is not None and 0.0 < p < 1.0 and q > 0.0:
            out[p] = q
    return out


def _top(book: Mapping[str, dict[float, float]]) -> tuple[float, float, float, float] | None:
    bids, asks = book["bids"], book["asks"]
    if not bids or not asks:
        return None
    b, a = max(bids), min(asks)
    if a < b:
        return None
    return b, bids[b], a, asks[a]


def _depth(book: Mapping[str, dict[float, float]], side: str, n: int) -> float:
    levels = book[side]
    prices = sorted(levels, reverse=side == "bids")[:n]
    return sum(max(0.0, levels[p]) for p in prices)


def _imbalance(bid_depth: float, ask_depth: float) -> float:
    total = bid_depth + ask_depth
    return (bid_depth - ask_depth) / total if total > 1e-12 else 0.0


def _ofi(old: tuple[float, float, float, float] | None, new: tuple[float, float, float, float] | None) -> float:
    if old is None or new is None:
        return 0.0
    ob, obq, oa, oaq = old
    nb, nbq, na, naq = new
    if nb > ob:
        bid_flow = nbq
    elif nb == ob:
        bid_flow = nbq - obq
    else:
        bid_flow = -obq
    if na < oa:
        ask_flow = -naq
    elif na == oa:
        ask_flow = oaq - naq
    else:
        ask_flow = oaq
    return bid_flow + ask_flow


def _snapshot(
    token: str, book: Mapping[str, dict[float, float]], exchange_ts_ms: int,
    received_ts_ms: int, ofi_l1: float,
) -> dict[str, object] | None:
    top = _top(book)
    if top is None:
        return None
    bid, bid_q, ask, ask_q = top
    d1b, d1a = bid_q, ask_q
    d3b, d3a = _depth(book, "bids", 3), _depth(book, "asks", 3)
    d5b, d5a = _depth(book, "bids", 5), _depth(book, "asks", 5)
    mid = 0.5 * (bid + ask)
    return {
        "exchange_ts_ms": exchange_ts_ms,
        "received_ts_ms": received_ts_ms,
        "market_id": token,
        "mid": mid,
        "spread": ask - bid,
        "microprice": microprice(bid, ask, d1b, d1a),
        "bid_depth_l1": d1b,
        "ask_depth_l1": d1a,
        "bid_depth_l3": d3b,
        "ask_depth_l3": d3a,
        "bid_depth_l5": d5b,
        "ask_depth_l5": d5a,
        "imbalance_l1": _imbalance(d1b, d1a),
        "imbalance_l3": _imbalance(d3b, d3a),
        "imbalance_l5": _imbalance(d5b, d5a),
        "ofi_l1": ofi_l1,
        "feed_latency_ms": max(0, received_ts_ms - exchange_ts_ms) if exchange_ts_ms > 0 else 0,
    }


def replay_capture(path: Path, output_features: Path | None = None) -> list[dict[str, object]]:
    books: dict[str, dict[str, dict[float, float]]] = {}
    rows: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                wrapper = json.loads(line)
            except json.JSONDecodeError:
                continue
            received = int(wrapper.get("received_ts_ms", 0) or 0)
            payload = wrapper.get("payload")
            events = payload if isinstance(payload, list) else [payload]
            for event in events:
                if not isinstance(event, dict):
                    continue
                kind = str(event.get("event_type") or event.get("type") or "")
                exchange = _timestamp_ms(event.get("timestamp"))
                if kind == "book":
                    token = str(event.get("asset_id") or "")
                    if not token:
                        continue
                    old = _top(books[token]) if token in books else None
                    books[token] = {"bids": _levels(event.get("bids")), "asks": _levels(event.get("asks"))}
                    new = _top(books[token])
                    row = _snapshot(token, books[token], exchange, received, _ofi(old, new))
                    if row is not None:
                        rows.append(row)
                elif kind == "price_change" and isinstance(event.get("price_changes"), list):
                    for change in event["price_changes"]:
                        if not isinstance(change, dict):
                            continue
                        token = str(change.get("asset_id") or "")
                        if not token or token not in books:
                            continue
                        old = _top(books[token])
                        price, size = _finite(change.get("price")), _finite(change.get("size"))
                        if price is None or size is None:
                            continue
                        side = "bids" if str(change.get("side", "")).upper() == "BUY" else "asks"
                        if size <= 0.0:
                            books[token][side].pop(price, None)
                        else:
                            books[token][side][price] = size
                        new = _top(books[token])
                        row = _snapshot(token, books[token], exchange, received, _ofi(old, new))
                        if row is not None:
                            rows.append(row)
    rows.sort(key=lambda row: (int(row["exchange_ts_ms"]), str(row["market_id"]), int(row["received_ts_ms"])))
    if output_features:
        output_features.parent.mkdir(parents=True, exist_ok=True)
        with output_features.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FEATURE_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
    return rows


def _load_snapshots(path: Path) -> list[dict[str, object]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS.difference(reader.fieldnames or [])
        if missing:
            raise ValueError("missing columns: " + ", ".join(sorted(missing)))
        rows: list[dict[str, object]] = []
        for raw in reader:
            parsed: dict[str, object] = {"market_id": str(raw.get("market_id", ""))}
            try:
                parsed["exchange_ts_ms"] = int(float(raw.get("exchange_ts_ms", "0")))
            except (TypeError, ValueError):
                continue
            ok = bool(parsed["market_id"])
            for name in REQUIRED_COLUMNS.difference({"exchange_ts_ms", "market_id"}):
                value = _finite(raw.get(name))
                if value is None:
                    ok = False
                    break
                parsed[name] = value
            if ok:
                rows.append(parsed)
    rows.sort(key=lambda row: (int(row["exchange_ts_ms"]), str(row["market_id"])))
    return rows


@dataclass(frozen=True)
class LabeledRow:
    ts_ms: int
    market_id: str
    mid: float
    spread: float
    features: tuple[float, ...]
    future_move: float


def label_future_markout(
    snapshots: Sequence[Mapping[str, object]], horizon_ms: int, max_lag_ms: int | None = None
) -> list[LabeledRow]:
    if horizon_ms <= 0:
        raise ValueError("horizon_ms must be positive")
    tolerance = max_lag_ms if max_lag_ms is not None else max(1000, horizon_ms // 2)
    by_market: dict[str, list[Mapping[str, object]]] = {}
    for row in snapshots:
        by_market.setdefault(str(row["market_id"]), []).append(row)
    labeled: list[LabeledRow] = []
    for market_id, rows in by_market.items():
        rows = sorted(rows, key=lambda row: int(row["exchange_ts_ms"]))
        j = 0
        for i, row in enumerate(rows):
            ts = int(row["exchange_ts_ms"])
            if ts <= 0:
                continue
            target = ts + horizon_ms
            j = max(j, i + 1)
            while j < len(rows) and int(rows[j]["exchange_ts_ms"]) < target:
                j += 1
            if j >= len(rows):
                break
            future_ts = int(rows[j]["exchange_ts_ms"])
            if future_ts - target > tolerance:
                continue
            mid, future_mid = float(row["mid"]), float(rows[j]["mid"])
            labeled.append(LabeledRow(
                ts, market_id, mid, float(row["spread"]),
                (
                    float(row["microprice"]) - mid,
                    float(row["imbalance_l1"]), float(row["imbalance_l3"]),
                    float(row["imbalance_l5"]), float(row["ofi_l1"]),
                    float(row["spread"]), float(row["feed_latency_ms"]),
                ),
                future_mid - mid,
            ))
    labeled.sort(key=lambda row: (row.ts_ms, row.market_id))
    return labeled


def _solve(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    n = len(rhs)
    a = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            raise ValueError("singular regression system")
        a[col], a[pivot] = a[pivot], a[col]
        scale = a[col][col]
        a[col] = [v / scale for v in a[col]]
        for row in range(n):
            if row == col:
                continue
            factor = a[row][col]
            a[row] = [a[row][k] - factor * a[col][k] for k in range(n + 1)]
    return [a[i][-1] for i in range(n)]


def _standardize(train: Sequence[Sequence[float]], test: Sequence[Sequence[float]]) -> tuple[list[list[float]], list[list[float]]]:
    p = len(train[0])
    means = [sum(row[j] for row in train) / len(train) for j in range(p)]
    scales = []
    for j in range(p):
        var = sum((row[j] - means[j]) ** 2 for row in train) / max(1, len(train) - 1)
        scales.append(max(1e-9, math.sqrt(var)))
    def transform(rows: Sequence[Sequence[float]]) -> list[list[float]]:
        return [[(row[j] - means[j]) / scales[j] for j in range(p)] for row in rows]
    return transform(train), transform(test)


def fit_ridge(train_x: Sequence[Sequence[float]], train_y: Sequence[float], l2: float = 1.0) -> list[float]:
    if len(train_x) != len(train_y) or not train_x:
        raise ValueError("non-empty aligned training data required")
    p = len(train_x[0]) + 1
    gram, rhs = [[0.0] * p for _ in range(p)], [0.0] * p
    for x, y in zip(train_x, train_y):
        row = [1.0, *x]
        for i in range(p):
            rhs[i] += row[i] * y
            for j in range(p):
                gram[i][j] += row[i] * row[j]
    for j in range(1, p):
        gram[j][j] += max(0.0, l2)
    return _solve(gram, rhs)


def predict(beta: Sequence[float], x: Sequence[float]) -> float:
    return beta[0] + sum(c * v for c, v in zip(beta[1:], x))


def _mse(actual: Sequence[float], forecast: Sequence[float]) -> float:
    return sum((a - f) ** 2 for a, f in zip(actual, forecast)) / max(1, len(actual))


def _sign_accuracy(actual: Sequence[float], forecast: Sequence[float]) -> float:
    usable = [(a, f) for a, f in zip(actual, forecast) if abs(a) > 1e-12 and abs(f) > 1e-12]
    return sum((a > 0) == (f > 0) for a, f in usable) / len(usable) if usable else 0.0


def _economic_score(rows: Sequence[LabeledRow], forecasts: Sequence[float], multiplier: float, slippage_bps: float) -> dict[str, float | int]:
    pnls: list[float] = []
    for row, forecast in zip(rows, forecasts):
        cost = multiplier * (0.5 * max(0.0, row.spread) + max(0.0, row.mid) * max(0.0, slippage_bps) / 10000.0)
        if abs(forecast) <= cost:
            continue
        pnls.append((1.0 if forecast > 0 else -1.0) * row.future_move - cost)
    return {
        "trades": len(pnls),
        "net_markout_per_share": sum(pnls),
        "mean_net_markout_per_share": sum(pnls) / len(pnls) if pnls else 0.0,
        "positive_share": sum(p > 0 for p in pnls) / len(pnls) if pnls else 0.0,
    }


def evaluate_challenger(labeled: Sequence[LabeledRow], train_fraction: float = 0.60, l2: float = 2.0, slippage_bps: float = 2.0) -> dict[str, object]:
    if len(labeled) < 40:
        return {"evidence_state": "MORE_EVIDENCE_REQUIRED", "reason": "fewer_than_40_labeled_rows", "rows": len(labeled)}
    split = max(20, min(len(labeled) - 10, int(len(labeled) * train_fraction)))
    train, test = list(labeled[:split]), list(labeled[split:])
    y_train, y_test = [r.future_move for r in train], [r.future_move for r in test]
    x_train, x_test = _standardize([r.features for r in train], [r.features for r in test])
    b_train, b_test = _standardize([[r.features[0]] for r in train], [[r.features[0]] for r in test])
    base_beta, challenger_beta = fit_ridge(b_train, y_train, l2), fit_ridge(x_train, y_train, l2)
    base_pred = [predict(base_beta, x) for x in b_test]
    challenger_pred = [predict(challenger_beta, x) for x in x_test]
    base_mse, challenger_mse = _mse(y_test, base_pred), _mse(y_test, challenger_pred)
    stress = {str(m): _economic_score(test, challenger_pred, m, slippage_bps) for m in (1.0, 1.5, 2.0)}
    preliminary = len(test) >= 100 and challenger_mse < base_mse and all(int(v["trades"]) >= 10 and float(v["net_markout_per_share"]) > 0 for v in stress.values())
    return {
        "evidence_state": "PRELIMINARY_SIGNAL" if preliminary else "MORE_EVIDENCE_REQUIRED",
        "rows": len(labeled), "train_rows": len(train), "test_rows": len(test),
        "features": list(FEATURES),
        "baseline": {"name": "microprice_displacement_only", "mse": base_mse, "sign_accuracy": _sign_accuracy(y_test, base_pred)},
        "challenger": {
            "name": "causal_microprice_imbalance_ofi_ridge", "mse": challenger_mse,
            "sign_accuracy": _sign_accuracy(y_test, challenger_pred),
            "mse_improvement_fraction": (base_mse - challenger_mse) / base_mse if base_mse > 0 else 0.0,
        },
        "cost_stress": stress,
        "promotion_note": "A single shadow window can never authorize integration; require independent sessions and fill-conditioned markout.",
    }


def render_report(result: Mapping[str, object], invariant: Mapping[str, object]) -> str:
    lines = [
        "# V5 causal microstructure research", "",
        f"- evidence state: `{result.get('evidence_state', 'MORE_EVIDENCE_REQUIRED')}`",
        f"- mirrored-book no-positive-taker-edge invariant: `{invariant['no_positive_taker_edge']}`",
        f"- incumbent YES fixture gross edge: `{float(invariant['yes_taker_gross_edge']):.8f}`",
        f"- incumbent NO fixture gross edge: `{float(invariant['no_taker_gross_edge']):.8f}`",
        "- causal target: future token midpoint markout, not terminal event resolution",
    ]
    if "challenger" in result:
        baseline, challenger = result["baseline"], result["challenger"]
        assert isinstance(baseline, Mapping) and isinstance(challenger, Mapping)
        lines += ["", "## Common chronological comparison",
                  f"- labeled rows: `{result['rows']}`; test rows: `{result['test_rows']}`",
                  f"- microprice-only MSE: `{float(baseline['mse']):.10g}`",
                  f"- causal-feature MSE: `{float(challenger['mse']):.10g}`",
                  f"- MSE improvement: `{100.0 * float(challenger['mse_improvement_fraction']):.2f}%`",
                  f"- causal-feature sign accuracy: `{100.0 * float(challenger['sign_accuracy']):.2f}%`"]
        stress = result.get("cost_stress", {})
        if isinstance(stress, Mapping):
            for name, value in stress.items():
                if isinstance(value, Mapping):
                    lines.append(f"- {name}x cost: trades `{value['trades']}`, net markout/share `{float(value['net_markout_per_share']):.8g}`")
    elif "reason" in result:
        lines += ["", f"- blocker: `{result['reason']}`; labeled rows: `{result.get('rows', 0)}`"]
    lines += ["", "Research-only result. No champion, capital, risk, OOS, credential or order-submission change is authorized."]
    return "\n".join(lines) + "\n"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--features", type=Path)
    source.add_argument("--capture", type=Path)
    parser.add_argument("--horizon-ms", type=int, default=5000)
    parser.add_argument("--max-lag-ms", type=int, default=None)
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    parser.add_argument("--output-features", type=Path)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-markdown", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    invariant = mirrored_binary_invariant()
    if args.capture:
        snapshots = replay_capture(args.capture, args.output_features)
    elif args.features:
        snapshots = _load_snapshots(args.features)
    else:
        snapshots = []
    if not snapshots:
        result: dict[str, object] = {"evidence_state": "MORE_EVIDENCE_REQUIRED", "reason": "live_event_time_feature_file_not_supplied", "rows": 0}
    else:
        labeled = label_future_markout(snapshots, args.horizon_ms, args.max_lag_ms)
        result = evaluate_challenger(labeled, slippage_bps=args.slippage_bps)
        result["feature_rows"] = len(snapshots)
    payload = {"schema_version": 2, "invariant": invariant, "evaluation": result}
    report = render_report(result, invariant)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.output_markdown:
        args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
        args.output_markdown.write_text(report, encoding="utf-8")
    print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
