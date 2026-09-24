"""CTest: raw public WS fixture -> real C++ decoder -> causal N-leg scenario.

All inputs are synthetic. This proves the cross-language boundary, not fills.
"""
from copy import deepcopy
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path[:0]=[str(Path(__file__).parent),str(Path(__file__).resolve().parents[1]/"scripts")]
from test_v7_exact_arb_native_evidence import data, MODEL
from test_v7_exact_arb_native_execution_bridge import native
from test_v7_exact_arb_native_arrival import inputs
from v7_exact_arb_native_evidence import SAFETY, canonical
from v7_exact_arb_native_arrival import NativeArrivalHistory
from v7_unified_exact_arb_graph_execution_shadow import simulate
from test_v7_exact_arb_native_run import study, competing_unwinds, recycling_study
from v7_exact_arb_native_run import run


def replay_fixture(config,root,binary):
    expected=[json.loads(line) for line in config["replay"].read_text().splitlines()][:-1]
    capture=[]
    for frame in expected:
        payload=[{"event_type":"book","asset_id":book["token_id"],
            "timestamp":frame["receive_monotonic_ns"]//1000000,
            "asks":[{"price":f"{p/10000:.4f}","size":f"{q/1000000:.6f}"} for p,q in book["asks_e4_microshares"]],
            "bids":[{"price":f"{p/10000:.4f}","size":f"{q/1000000:.6f}"} for p,q in book["bids_e4_microshares"]]}
            for book in frame["books"]]
        wire=canonical(payload)
        capture.append({**SAFETY,"schema":"polymarket_v7_native_exact_arb_ws_frame_v1","model_sha":MODEL,
            "observer_session_id":frame["observer_session_id"],"session_manifest_sha256":frame["session_manifest_sha256"],
            "feed_frame_sequence":frame["feed_frame_sequence"],"connection_epoch":1,"source_frame_valid":True,
            "decoded_events":len(payload),"receive_wall_ms":frame["receive_monotonic_ns"]//1000000,
            "receive_monotonic_ns":frame["receive_monotonic_ns"],"decode_complete_monotonic_ns":frame["availability_monotonic_ns"],
            "graph_decision_start_ns":frame["graph_decision_start_ns"],"payload":wire,
            "payload_sha256":hashlib.sha256(wire.encode()).hexdigest()})
    (root/"study-frames.jsonl").write_text("".join(canonical(row)+"\n" for row in capture))
    replay=subprocess.run([binary,MODEL,str(config["manifest"]),str(root/"study-frames.jsonl")],
                          text=True,capture_output=True,timeout=20)
    assert replay.returncode==0,replay.stderr
    config["replay"].write_text(replay.stdout)


def main():
    with tempfile.TemporaryDirectory(prefix="native-arrival-roundtrip-") as temporary:
        root=Path(temporary)
        manifest,candidate,frame=inputs.__wrapped__(native.__wrapped__(data.__wrapped__(root)))
        snapshot=[{"event_type":"book","asset_id":b["token_id"],"timestamp":1000,
                   "bids":[{"price":"0.39","size":"5"}],"asks":[{"price":"0.40","size":"5"}]}
                  for b in frame["books"]]
        bad=deepcopy(snapshot)
        for b in bad: b["asks"][0]["price"]="0.60"
        payloads=[snapshot,bad,snapshot,[]]
        rows=[]
        for i,(payload,ns) in enumerate(zip(payloads,(1000000000,1000100000,1000300000,1010000000)),1):
            wire=canonical(payload)
            rows.append({**SAFETY,"schema":"polymarket_v7_native_exact_arb_ws_frame_v1","model_sha":MODEL,
                "observer_session_id":frame["observer_session_id"],"session_manifest_sha256":frame["session_manifest_sha256"],
                "feed_frame_sequence":i,"connection_epoch":1,"source_frame_valid":True,
                "decoded_events":len(payload),"receive_wall_ms":ns//1000000,"receive_monotonic_ns":ns,
                "decode_complete_monotonic_ns":ns,"graph_decision_start_ns":ns,
                "payload_sha256":hashlib.sha256(wire.encode()).hexdigest(),"payload":wire})
        (root/"session.json").write_bytes(manifest)
        (root/"frames.jsonl").write_text("".join(canonical(row)+"\n" for row in rows))
        command=[sys.argv[1],MODEL,str(root/"session.json"),str(root/"frames.jsonl")]
        output=subprocess.run(command,capture_output=True,text=True,timeout=20)
        assert output.returncode==0,output.stderr
        again=subprocess.run(command,capture_output=True,text=True,timeout=20)
        assert again.returncode==0 and again.stdout==output.stdout
        h=NativeArrivalHistory(manifest,MODEL)
        try:
            replayed=output.stdout.splitlines()
            for row in replayed[:-1]: h.ingest(row)
            h.seal(replayed[-1])
            view=h.for_candidate(candidate)
            result=simulate(candidate,view,"PARALLEL",Fraction(1,5),0)
            assert result["state"]!="ALL_LEGS_FILLED"
            assert len(result["legs"])==2 and all(r["filled_size"]=="0" for r in result["legs"])
            assert result["transport_delay_ms"]=="1/5"
            assert result["venue_execution_verified"] is False
            assert simulate(candidate,view,"PARALLEL",1,0)["state"]=="ALL_LEGS_FILLED"
        finally: h.close()
        # A partially emitted stdout stream without a success receipt is not a
        # sealed history. CLI must fail on an incomplete final JSONL record.
        with (root/"frames.jsonl").open("a") as stream: stream.write('{"truncated":')
        broken=subprocess.run(command,capture_output=True,text=True,timeout=20)
        assert broken.returncode!=0 and "unsealed_or_unreadable_segment" in broken.stderr
        assert "ws_replay_receipt_v1" not in broken.stdout

        # Complete runner: native decoder output + negative-to-positive episode
        # + pinned full evidence -> immutable multi-arm report, not just an API.
        native_fixture=native.__wrapped__(data.__wrapped__(root))
        config=study.__wrapped__(native_fixture,inputs.__wrapped__(native_fixture),root)
        replay_fixture(config,root,sys.argv[1])
        report_path=run(**config)
        report=json.loads(report_path.read_text())
        assert report["episode_admission"]["observed_episode_starts"]==1
        assert len(report["arms"])==3
        assert {state for arm in report["arms"] for state in arm["states"]}=={"NO_LEGS_FILLED","ALL_LEGS_FILLED","CENSORED"}
        assert report["economic_evidence"]["net_pnl"] is None
        (root/"arms.json").write_text(canonical(config["arms"]))
        runner=Path(__file__).resolve().parents[1]/"scripts"/"v7_exact_arb_native_run.py"
        cli=subprocess.run([sys.executable,str(runner),"--model-sha",MODEL,"--session-manifest",str(config["manifest"]),
            "--replay-output",str(config["replay"]),"--observations",str(config["observations"][0]),
            "--full-evidence",str(config["full_evidence"][0]),"--bundles",str(config["bundles"]),
            "--output",str(config["output"]),"--arms",str(root/"arms.json"),
            "--control-events",str(config["control_events"][0])],capture_output=True,text=True,timeout=20)
        assert cli.returncode==0,cli.stderr
        assert cli.stdout.strip()==str(report_path)
        config=competing_unwinds(config)
        replay_fixture(config,root,sys.argv[1])
        shared_path=run(**config)
        shared=[json.loads(line) for line in (shared_path.parent/"shared_scenarios.jsonl").read_text().splitlines()]
        assert sorted(row["state"] for row in shared)==["EXPOSURE_REMAINS","PARTIAL_UNWOUND"]

        # The same native-decoded stream must fund decisions using each world's
        # own known ACKs, never another arm's earlier release or future outcome.
        capital_root=root/"capital";capital_root.mkdir()
        native_fixture=native.__wrapped__(data.__wrapped__(capital_root))
        config=recycling_study(study.__wrapped__(native_fixture,inputs.__wrapped__(native_fixture),capital_root))
        replay_fixture(config,capital_root,sys.argv[1])
        capital_report=json.loads(run(**config).read_text())
        arms={int(a["ack_delay_ms"]):a["arm_id"] for a in capital_report["arms"]}
        worlds={w["arm_id"]:w["capital_lifecycle"] for w in capital_report["shared_execution"]["worlds"]}
        assert worlds[arms[0]]["closed_order_groups"]==2
        assert worlds[arms[0]]["modeled_balances"]["PUSD"]=="1/10"
        assert worlds[arms[5]]["closed_order_groups"]==1
        assert worlds[arms[5]]["modeled_balances"]["PUSD"]=="21/5"
        assert all(w["portfolio_net_pnl"] is None and not w["settlement_release_verified"] for w in worlds.values())


if __name__=="__main__": main()
