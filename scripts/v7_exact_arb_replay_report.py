#!/usr/bin/env python3
"""Replay causal local observations into the graph; retain evidence limitations."""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from v7_unified_exact_arb_graph import load
from v7_unified_exact_arb_graph_shadow import Shadow, atomic
from v7_exact_arb_causal import ReconstructedDepth
from v7_unified_exact_arb_graph import sha


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ("graph","tape","capital-policy","output","opportunities"):
        p.add_argument("--"+name,type=Path,required=True)
    p.add_argument("--champion-status",type=Path)
    p.add_argument("--delta-tape",type=Path)
    p.add_argument("--model-sha",required=True)
    a=p.parse_args()
    s=Shadow(SimpleNamespace(**vars(a),status=a.output,interval_ms=10))
    s.graph()
    if s.graph_state!="COLLECTING":raise SystemExit(s.graph_state)
    first=None;lines=0
    reconstruction=ReconstructedDepth(a.model_sha)
    with a.tape.open() as tape:
        for line in tape:
            if not line.endswith("\n"):break
            row=json.loads(line);lines+=1
            if first is None:first=int(row["receive_wall_ms"])
            if a.delta_tape:reconstruction.anchor(row)
            else:s.update(row)
    if a.delta_tape:
      paths=sorted(a.delta_tape.parent.glob("*.segment-*.jsonl"))+[a.delta_tape]
      for path in paths:
        with path.open() as tape:
            for line in tape:
                if not line.endswith("\n"):break
                row=json.loads(line)
                now,books=reconstruction.delta(row)
                s.update_decoded(now,books,sha(row))
    result=s.status()
    duration=max(0,s.last_event_ms-(first or s.last_event_ms))
    result.update(evidence_scope="LOCAL_PUBLIC_WEBSOCKET_REPLAY_NOT_LONDON",observation_duration_ms=duration,
                  input_rows=lines,candidate_rate_per_hour=s.funnel["candidate_emitted"]*3600000/duration if duration else None,
                  champion_observer=load(a.champion_status) if a.champion_status else None,
                  timestamp_basis="LOCAL_RECEIVE_WALL_MS_WITH_PER_LEG_MONOTONIC_AGE",
                  software_state="RESEARCH_WORKTREE_NOT_AN_EXACT_SHA_RELEASE",
                  execution_outcomes="UNAVAILABLE_UNLESS_SEPARATE_EXECUTION_CYCLES_ARE_PRESENT")
    result["reconstruction_gaps"]=reconstruction.gaps
    result.update(state="REPLAY_COMPLETE",worker_health="STOPPED_AFTER_BOUNDED_REPLAY")
    atomic(a.output,result)
    print(json.dumps({"events":s.events_processed,"evaluations":s.funnel["relations_considered"],
                      "candidates":s.funnel["candidate_emitted"],"duration_ms":duration,"dropped":dict(s.dropped)}))


if __name__=="__main__":main()
