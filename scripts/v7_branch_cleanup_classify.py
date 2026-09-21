#!/usr/bin/env python3
"""Classify a closed PR list against an exact remote branch tip."""
from __future__ import annotations
import argparse
import json
import re
import sys

SHA40=re.compile(r"^[0-9a-f]{40}$")


def classify(rows, tip):
    if not SHA40.fullmatch(tip):
        raise ValueError("exact branch tip SHA required")
    for row in rows:
        if not isinstance(row,dict) or row.get("headRefOid") != tip:
            continue
        if row.get("mergedAt"):
            return "MERGED_EXACT"
        text=((row.get("title") or "")+"\n"+(row.get("body") or "")).lower()
        if "superseded" in text:
            return "SUPERSEDED_EXACT"
    return "NONE"


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tip",required=True)
    args=parser.parse_args(argv)
    rows=json.load(sys.stdin)
    if not isinstance(rows,list):
        raise ValueError("PR list required")
    print(classify(rows,args.tip))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
