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
    parser.add_argument("--code-sha")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    sha = args.code_sha or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if len(sha) != 40 or any(ch not in "0123456789abcdef" for ch in sha):
        raise ValueError("exact 40-char lowercase code SHA required")
    data = build_dataset(args.input_root, minimum_wall_ns=args.minimum_wall_ns,
                         settlement_root=args.settlement_root)
    predictions, fold_receipt = walk_forward(data["decisions"], desired_folds=args.folds)
    economics = economic_evaluation(predictions)
    publish(args.output, root=root, start_sha=sha, data=data, folds=fold_receipt, economics=economics)
    print("output=" + str(Path(args.output).resolve()) + " input_state=" + data["input_state"] + " oos=" + str(len(predictions)))


if __name__ == "__main__":
    main()
