#!/usr/bin/env python3
"""Run the native PureArb/graph benchmark; never substitute arithmetic baselines."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
from v7_unified_exact_arb_graph import SAFETY


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--native-binary",type=Path,default=Path("build-Release/pm_v7_exact_arb_graph_bench"))
    p.add_argument("--iterations",type=int,default=200000)
    p.add_argument("--output",type=Path)
    a=p.parse_args()
    if not 10000<=a.iterations<=10000000: raise SystemExit("iterations must be in [10000,10000000]")
    if not a.native_binary.is_file(): raise SystemExit("build the pm_v7_exact_arb_graph_bench CMake target first")
    result=subprocess.run([str(a.native_binary.resolve()),str(a.iterations)],check=True,capture_output=True,text=True)
    value={**json.loads(result.stdout),**SAFETY,
           "comparison_scope":"FROZEN_SWEEP_VS_VALIDATED_GRAPH_EVALUATION_NOT_VENUE_LATENCY"}
    body=json.dumps(value,sort_keys=True,indent=2)+"\n"
    if a.output:a.output.write_text(body)
    else:print(body,end="")
    return 0


if __name__=="__main__":raise SystemExit(main())
