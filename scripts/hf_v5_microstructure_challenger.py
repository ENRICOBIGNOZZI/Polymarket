#!/usr/bin/env python3
"""Research-only diagnostics for the V5 microstructure sleeve.

The active V5 `micro` child currently feeds a contemporaneous order-book
microprice into the terminal-probability engine. This module makes that semantic
contract explicit and provides a dependency-free chronological harness for a
proper seconds-to-minutes challenger whose target is future mid-price markout.

It never submits orders and it does not change production configuration.
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
    "exchange_ts_ms",
    "market_id",
    "mid",
    "spread",
    "microprice",
    "imbalance_l1",
    "imbalance_l3",
    "imbalance_l5",
    "ofi_l1",
    "feed_latency_ms",
}


def _finite(value: object) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def microprice(best_bid: float, best_ask: float, bid_depth: float, ask_depth: float) -> float:
    if not all(math.isfinite(x) for x in (best_bid, best_ask, bid_depth, ask_depth)):
        raise ValueError("microprice inputs must be finite")
    if best_ask < best_bid:
        raise ValueError("crossed book")
    bid_depth = max(0.0, bid_depth)
    ask_depth = max(0.0, ask_depth)
    if bid_depth + ask_depth <= 1e-12:
        return 0.5 * (best_bid + best_ask)
    value = (best_ask * bid_depth + best_bid * ask_depth) / (bid_depth + ask_depth)
    return min(best_ask, max(best_bid, value))


def incumbent_micro_fair(
    yes_micro: float,
    no_micro: float,
    yes_depth: float,
    no_depth: float,
    yes_spread: float,
    no_spread: float,
) -> float:
    wy = math.sqrt(max(0.0, yes_depth)) / (1.0 + 20.0 * max(0.0, yes_spread))
    wn = math.sqrt(max(0.0, no_depth)) / (1.0 + 20.0 * max(0.0, no_spread))
    if wy + wn <= 1e-12:
        return 0.5
    return min(0.999, max(0.001, (wy * yes_micro + wn * (1.0 - no_micro)) / (wy + wn)))


def mirrored_binary_invariant(
    yes_bid: float = 0.48,
    yes_ask: float = 0.50,
    yes_bid_depth: float = 100.0,
    yes_ask_depth: float = 50.0,
) -> dict[str, float | bool]:
    no_bid = 1.0 - yes_ask
    no_ask = 1.0 - yes_bid
    no_bid_depth = yes_ask_depth
    no_ask_depth = yes_bid_depth
    y = microprice(yes_bid, yes_ask, yes_bid_depth, yes_ask_depth)
    n = microprice(no_bid, no_ask, no_bid_depth, no_ask_depth)
    q = incumbent_micro_fair(
        y,
        n,
        yes_bid_depth + yes_ask_depth,
        no_bid_depth + no_ask_depth,
        yes_ask - yes_bid,
        no_ask - no_bid,
    )
    yes_edge = q - yes_ask
    no_edge = (1.0 - q) - no_ask
    return {
        "yes_microprice": y,
        "no_microprice": n,
        "complement_error": abs(y - (1.0 - n)),
        "incumbent_q_yes": q,
        "yes_taker_gross_edge": yes_edge,
        "no_taker_gross_edge": no_edge,
        "no_positive_taker_edge": yes_edge <= 1e-12 and no_edge <= 1e-12,
    }


@dataclass(frozen=True)
class LabeledRow:
    ts_ms: int
    market_id: str
    mid: float
    spread: float
    features: tuple[float, ...]
    future_move: float


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
        j = 0
        for i, row in enumerate(rows):
            ts = int(row["exchange_ts_ms"])
            target = ts + horizon_ms
            j = max(j, i + 1)
            while j < len(rows) and int(rows[j]["exchange_ts_ms"]) < target:
                j += 1
            if j >= len(rows):
                break
            future_ts = int(rows[j]["exchange_ts_ms"])
            if future_ts - target > tolerance:
                continue
            mid = float(row["mid"])
            future_mid = float(rows[j]["mid"])
            features = (
                float(row["microprice"]) - mid,
                float(row["imbalance_l1"]),
                float(row["imbalance_l3"]),
                float(row["imbalance_l5"]),
                float(row["ofi_l1"]),
                float(row["spread"]),
                float(row["feed_latency_ms"]),
            )
            labeled.append(LabeledRow(ts, market_id, mid, float(row["spread"]), features, future_mid - mid))
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
        a[col] = [value / scale for value in a[col]]
        for row in range(n):
            if row == col:
                continue
            factor = a[row][col]
            if abs(factor) <= 1e-18:
                continue
            a[row] = [a[row][k] - factor * a[col][k] for k in range(n + 1)]
    return [a[i][-1] for i in range(n)]


def _standardize(
    train: Sequence[Sequence[float]], test: Sequence[Sequence[float]]
) -> tuple[list[list[float]], list[list[float]]]:
    if not train:
        return [], []
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
    gram = [[0.0] * p for _ in range(p)]
    rhs = [0.0] * p
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
    return beta[0] + sum(coef * value for coef, value in zip(beta[1:], x))


def _mse(actual: Sequence[float], forecast: Sequence[float]) -> float:
    return sum((a - f) ** 2 for a, f in zip(actual, forecast)) / max(1, len(actual))


def _sign_accuracy(actual: Sequence[float], forecast: Sequence[float]) -> float:
    usable = [(a, f) for a, f in zip(actual, forecast) if abs(a) > 1e-12 and abs(f) > 1e-12]
    if not usable:
        return 0.0
    return sum(int((a > 0) == (f > 0)) for a, f in usable) / len(usable)


def _economic_score(
    rows: Sequence[LabeledRow], forecasts: Sequence[float], cost_multiplier: float, slippage_bps: float
) -> dict[str, float | int]:
    pnls: list[float] = []
    for row, forecast in zip(rows, forecasts):
        half_spread = 0.5 * max(0.0, row.spread)
        slippage = max(0.0, row.mid) * max(0.0, slippage_bps) / 10000.0
        cost = cost_multiplier * (half_spread + slippage)
        if abs(forecast) <= cost:
            continue
        direction = 1.0 if forecast > 0.0 else -1.0
        pnls.append(direction * row.future_move - cost)
    return {
        "trades": len(pnls),
        "net_markout_per_share": sum(pnls),
        "mean_net_markout_per_share": sum(pnls) / len(pnls) if pnls else 0.0,
        "positive_share": sum(pnl > 0 for pnl in pnls) / len(pnls) if pnls else 0.0,
    }


def evaluate_challenger(
    labeled: Sequence[LabeledRow], train_fraction: float = 0.60, l2: float = 2.0, slippage_bps: float = 2.0
) -> dict[str, object]:
    if not 0.25 <= train_fraction <= 0.90:
        raise ValueError("train_fraction must be in [0.25, 0.90]")
    if len(labeled) < 40:
        return {"evidence_state": "MORE_EVIDENCE_REQUIRED", "reason": "fewer_than_40_labeled_rows", "rows": len(labeled)}
    split = max(20, min(len(labeled) - 10, int(len(labeled) * train_fraction)))
    train, test = list(labeled[:split]), list(labeled[split:])
    x_train = [list(row.features) for row in train]
    x_test = [list(row.features) for row in test]
    xs_train, xs_test = _standardize(x_train, x_test)
    y_train = [row.future_move for row in train]
    y_test = [row.future_move for row in test]
    base_train, base_test = _standardize([[row.features[0]] for row in train], [[row.features[0]] for row in test])
    base_beta = fit_ridge(base_train, y_train, l2=l2)
    challenger_beta = fit_ridge(xs_train, y_train, l2=l2)
    base_pred = [predict(base_beta, row) for row in base_test]
    challenger_pred = [predict(challenger_beta, row) for row in xs_test]
    base_mse = _mse(y_test, base_pred)
    challenger_mse = _mse(y_test, challenger_pred)
    stress = {str(multiplier): _economic_score(test, challenger_pred, multiplier, slippage_bps) for multiplier in (1.0, 1.5, 2.0)}
    enough = len(test) >= 100
    predictive = challenger_mse + 1e-15 < base_mse
    economic = all(int(value["trades"]) >= 10 and float(value["net_markout_per_share"]) > 0.0 for value in stress.values())
    return {
        "evidence_state": "CHALLENGER_PROMISING" if enough and predictive and economic else "MORE_EVIDENCE_REQUIRED",
        "rows": len(labeled),
        "train_rows": len(train),
        "test_rows": len(test),
        "features": list(FEATURES),
        "baseline": {"name": "microprice_displacement_only", "mse": base_mse, "sign_accuracy": _sign_accuracy(y_test, base_pred)},
        "challenger": {
            "name": "causal_microprice_imbalance_ofi_ridge",
            "mse": challenger_mse,
            "sign_accuracy": _sign_accuracy(y_test, challenger_pred),
            "mse_improvement_fraction": (base_mse - challenger_mse) / base_mse if base_mse > 0 else 0.0,
        },
        "cost_stress": stress,
        "promotion_note": "Research result only; requires independent live event-time sessions and fill-conditioned markout before integration.",
    }


def render_report(result: Mapping[str, object], invariant: Mapping[str, object]) -> str:
    lines = [
        "# V5 causal microstructure research",
        "",
        f"- evidence state: `{result.get('evidence_state', 'MORE_EVIDENCE_REQUIRED')}`",
        f"- incumbent mirrored-book no-positive-taker-edge invariant: `{invariant['no_positive_taker_edge']}`",
        f"- incumbent YES taker gross edge in fixture: `{float(invariant['yes_taker_gross_edge']):.8f}`",
        f"- incumbent NO taker gross edge in fixture: `{float(invariant['no_taker_gross_edge']):.8f}`",
        "- target for the challenger: future mid-price markout, not terminal event resolution",
    ]
    if "challenger" in result:
        baseline = result["baseline"]
        challenger = result["challenger"]
        assert isinstance(baseline, Mapping) and isinstance(challenger, Mapping)
        lines += [
            "",
            "## Common chronological comparison",
            f"- labeled rows: `{result['rows']}`; test rows: `{result['test_rows']}`",
            f"- microprice-only MSE: `{float(baseline['mse']):.10g}`",
            f"- causal feature MSE: `{float(challenger['mse']):.10g}`",
            f"- MSE improvement: `{100.0 * float(challenger['mse_improvement_fraction']):.2f}%`",
        ]
    elif "reason" in result:
        lines += ["", f"- blocker: `{result['reason']}`; labeled rows: `{result.get('rows', 0)}`"]
    lines += [
        "",
        "The active V5 micro expert is a contemporaneous fair-probability proxy. This harness deliberately evaluates a different economic object: causal seconds-to-minutes markout prediction on event-time rows. No live-model change is authorized by this report.",
    ]
    return "\n".join(lines) + "\n"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, help="Event-time microstructure feature CSV")
    parser.add_argument("--horizon-ms", type=int, default=5000)
    parser.add_argument("--max-lag-ms", type=int, default=None)
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-markdown", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    invariant = mirrored_binary_invariant()
    if args.features is None:
        result: dict[str, object] = {"evidence_state": "MORE_EVIDENCE_REQUIRED", "reason": "live_event_time_feature_file_not_supplied", "rows": 0}
    else:
        snapshots = _load_snapshots(args.features)
        labeled = label_future_markout(snapshots, args.horizon_ms, args.max_lag_ms)
        result = evaluate_challenger(labeled, slippage_bps=args.slippage_bps)
    payload = {"schema_version": 1, "invariant": invariant, "evaluation": result}
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
