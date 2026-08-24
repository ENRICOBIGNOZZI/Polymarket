#!/usr/bin/env python3
"""Purged chronological evaluation for the research-only V5 HF challenger."""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "hf_v5_microstructure_challenger_base",
    ROOT / "scripts" / "hf_v5_microstructure_challenger.py",
)
assert SPEC and SPEC.loader
base = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = base
SPEC.loader.exec_module(base)


def purged_split(
    labeled: Sequence[object],
    horizon_ms: int,
    tolerance_ms: int,
    train_fraction: float = 0.60,
) -> tuple[list[object], list[object], int]:
    if not 0.0 < train_fraction < 1.0:
        raise ValueError("train_fraction must lie in (0, 1)")
    rows = sorted(labeled, key=lambda row: (int(row.ts_ms), str(row.market_id)))
    if len(rows) < 2:
        return [], [], 0
    split_index = max(1, min(len(rows) - 1, int(len(rows) * train_fraction)))
    cutoff = int(rows[split_index].ts_ms)
    embargo = horizon_ms + max(0, tolerance_ms)
    train = [row for row in rows if int(row.ts_ms) + embargo < cutoff]
    test = [row for row in rows if int(row.ts_ms) >= cutoff]
    return train, test, cutoff


def evaluate_purged(
    labeled: Sequence[object],
    horizon_ms: int,
    tolerance_ms: int,
    train_fraction: float = 0.60,
    l2: float = 2.0,
    slippage_bps: float = 2.0,
) -> dict[str, object]:
    train, test, cutoff = purged_split(labeled, horizon_ms, tolerance_ms, train_fraction)
    if len(train) < 20 or len(test) < 10:
        return {
            "evidence_state": "MORE_EVIDENCE_REQUIRED",
            "reason": "insufficient_rows_after_purge",
            "rows": len(labeled),
            "train_rows": len(train),
            "test_rows": len(test),
            "cutoff_ts_ms": cutoff,
            "embargo_ms": horizon_ms + max(0, tolerance_ms),
        }

    y_train = [float(row.future_move) for row in train]
    y_test = [float(row.future_move) for row in test]
    x_train, x_test = base._standardize([row.features for row in train], [row.features for row in test])
    b_train, b_test = base._standardize([[row.features[0]] for row in train], [[row.features[0]] for row in test])
    base_beta = base.fit_ridge(b_train, y_train, l2)
    challenger_beta = base.fit_ridge(x_train, y_train, l2)
    base_pred = [base.predict(base_beta, x) for x in b_test]
    challenger_pred = [base.predict(challenger_beta, x) for x in x_test]
    base_mse = base._mse(y_test, base_pred)
    challenger_mse = base._mse(y_test, challenger_pred)
    stress = {
        str(multiplier): base._economic_score(test, challenger_pred, multiplier, slippage_bps)
        for multiplier in (1.0, 1.5, 2.0)
    }
    preliminary = (
        len(test) >= 100
        and challenger_mse < base_mse
        and all(
            int(value["trades"]) >= 10 and float(value["net_markout_per_share"]) > 0.0
            for value in stress.values()
        )
    )
    return {
        "evidence_state": "PRELIMINARY_SIGNAL" if preliminary else "MORE_EVIDENCE_REQUIRED",
        "rows": len(labeled),
        "train_rows": len(train),
        "test_rows": len(test),
        "cutoff_ts_ms": cutoff,
        "embargo_ms": horizon_ms + max(0, tolerance_ms),
        "features": list(base.FEATURES),
        "baseline": {
            "name": "microprice_displacement_only",
            "mse": base_mse,
            "sign_accuracy": base._sign_accuracy(y_test, base_pred),
        },
        "challenger": {
            "name": "causal_microprice_imbalance_ofi_ridge",
            "mse": challenger_mse,
            "sign_accuracy": base._sign_accuracy(y_test, challenger_pred),
            "mse_improvement_fraction": (base_mse - challenger_mse) / base_mse if base_mse > 0 else 0.0,
        },
        "cost_stress": stress,
        "promotion_note": "Purging prevents target overlap across the split; independent sessions and fill-conditioned evidence remain mandatory.",
    }


def render(result: Mapping[str, object], horizon_ms: int) -> str:
    lines = [
        f"# Purged V5 HF markout evaluation — {horizon_ms} ms",
        "",
        f"- evidence state: `{result.get('evidence_state', 'MORE_EVIDENCE_REQUIRED')}`",
        f"- labeled rows: `{result.get('rows', 0)}`",
        f"- train rows after purge: `{result.get('train_rows', 0)}`",
        f"- test rows: `{result.get('test_rows', 0)}`",
        f"- embargo: `{result.get('embargo_ms', 0)} ms`",
    ]
    if "challenger" in result:
        baseline = result["baseline"]
        challenger = result["challenger"]
        assert isinstance(baseline, Mapping) and isinstance(challenger, Mapping)
        lines += [
            f"- microprice-only MSE: `{float(baseline['mse']):.10g}`",
            f"- causal-feature MSE: `{float(challenger['mse']):.10g}`",
            f"- MSE improvement: `{100.0 * float(challenger['mse_improvement_fraction']):.2f}%`",
            f"- causal-feature sign accuracy: `{100.0 * float(challenger['sign_accuracy']):.2f}%`",
        ]
        stress = result.get("cost_stress", {})
        if isinstance(stress, Mapping):
            for name, value in stress.items():
                if isinstance(value, Mapping):
                    lines.append(
                        f"- {name}x cost: trades `{value['trades']}`, net markout/share `{float(value['net_markout_per_share']):.8g}`"
                    )
    elif "reason" in result:
        lines.append(f"- blocker: `{result['reason']}`")
    lines += ["", "Research-only. No production or risk mutation is authorized."]
    return "\n".join(lines) + "\n"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--capture", type=Path)
    source.add_argument("--features", type=Path)
    parser.add_argument("--horizon-ms", type=int, default=5000)
    parser.add_argument("--max-lag-ms", type=int, default=None)
    parser.add_argument("--train-fraction", type=float, default=0.60)
    parser.add_argument("--l2", type=float, default=2.0)
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    parser.add_argument("--output-features", type=Path)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-markdown", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.capture:
        snapshots = base.replay_capture(args.capture, args.output_features)
    else:
        snapshots = base._load_snapshots(args.features)
    tolerance = args.max_lag_ms if args.max_lag_ms is not None else max(1000, args.horizon_ms // 2)
    labeled = base.label_future_markout(snapshots, args.horizon_ms, tolerance)
    result = evaluate_purged(
        labeled,
        horizon_ms=args.horizon_ms,
        tolerance_ms=tolerance,
        train_fraction=args.train_fraction,
        l2=args.l2,
        slippage_bps=args.slippage_bps,
    )
    result["feature_rows"] = len(snapshots)
    payload = {"schema_version": 1, "horizon_ms": args.horizon_ms, "evaluation": result}
    report = render(result, args.horizon_ms)
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
