"""Run immutable offline HISTORICAL_WALK_FORWARD_V2."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess

from .core import DEFAULT_EPOCH_NS, build_dataset, economic_evaluation, walk_forward
from .report import publish


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--minimum-wall-ns", type=int, default=DEFAULT_EPOCH_NS)
    parser.add_argument("--settlement-root")
    parser.add_argument("--folds", type=int, default=3)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    data = build_dataset(args.input_root, minimum_wall_ns=args.minimum_wall_ns,
                         settlement_root=args.settlement_root)
    predictions, fold_receipt = walk_forward(data["decisions"], desired_folds=args.folds)
    economics = economic_evaluation(predictions)
    publish(args.output, root=root, start_sha=sha, data=data, folds=fold_receipt, economics=economics)
    print("output=" + str(Path(args.output).resolve()) + " input_state=" + data["input_state"] + " oos=" + str(len(predictions)))


if __name__ == "__main__":
    main()
