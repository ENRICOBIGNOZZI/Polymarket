"""Run immutable offline HISTORICAL_WALK_FORWARD_V2."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import subprocess

from .core import DEFAULT_EPOCH_NS, build_dataset, economic_evaluation, fit_full_repricing, walk_forward
from .report import publish


def load_capital_sizing_contract(root: Path) -> dict:
    risk = json.loads((root / "config/v7_native_risk_policy.json").read_text(encoding="utf-8"))
    signals = json.loads((root / "config/v7_crypto_signal_policy.json").read_text(encoding="utf-8"))
    contexts = signals.get("contexts") if isinstance(signals, dict) else None
    if not isinstance(contexts, dict) or not contexts:
        raise ValueError("signal policy contexts unavailable")
    total = min(
        int(risk["sleeve_budget_microdollars"]),
        int(risk["max_total_exposure_microdollars"]),
    )
    count = len(contexts)
    partition = total // count
    fraction = float(risk["target_order_fraction_of_context"])
    cap = int(risk["target_order_notional_cap_microdollars"])
    max_order = int(risk["max_single_order_microdollars"])
    max_market = int(risk["max_market_exposure_microdollars"])
    target = min(
        partition,
        cap,
        max_order,
        max_market,
        int(math.floor(partition * fraction + 1e-9)),
    )
    if min(total, count, partition, target) <= 0:
        raise ValueError("capital sizing contract invalid")
    return {
        "schema": "historical_walk_forward_v2_capital_sizing_contract_v1",
        "source": "config/v7_native_risk_policy.json+config/v7_crypto_signal_policy.json",
        "paper_only": True,
        "context_count": count,
        "global_budget_microdollars": total,
        "partition_microdollars": partition,
        "target_order_fraction_of_context": fraction,
        "target_order_notional_cap_microdollars": cap,
        "max_single_order_microdollars": max_order,
        "max_market_exposure_microdollars": max_market,
        "target_notional_microdollars": target,
        "historical_carryover_mode": "UNAVAILABLE_RESEARCH_APPROXIMATION",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--minimum-wall-ns", type=int, default=DEFAULT_EPOCH_NS)
    parser.add_argument("--settlement-root")
    parser.add_argument("--folds", type=int, default=3)
    parser.add_argument("--code-sha")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    sha = args.code_sha or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if len(sha) != 40 or any(ch not in "0123456789abcdef" for ch in sha):
        raise ValueError("exact 40-char lowercase code SHA required")
    data = build_dataset(args.input_root, minimum_wall_ns=args.minimum_wall_ns,
                         settlement_root=args.settlement_root)
    predictions, fold_receipt = walk_forward(data["decisions"], desired_folds=args.folds)
    capital_contract = load_capital_sizing_contract(root)
    economics = economic_evaluation(
        predictions, capital_sizing_contract=capital_contract)
    final_models = fit_full_repricing(data["decisions"])
    publish(args.output, root=root, start_sha=sha, data=data, folds=fold_receipt,
            economics=economics, final_models=final_models)
    print("output=" + str(Path(args.output).resolve()) + " input_state=" + data["input_state"] + " oos=" + str(len(predictions)))


if __name__ == "__main__":
    main()
