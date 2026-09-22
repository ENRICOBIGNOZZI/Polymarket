from __future__ import annotations
import importlib.util
import struct
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location("trace_summary",ROOT/"scripts/v7_latency_trace_summary.py")
MOD=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(MOD)

def test_binary_trace_summary_ignores_missing_stages():
    with tempfile.TemporaryDirectory() as d:
        path=Path(d)/"trace.bin"
        header=MOD.HEADER.pack(MOD.MAGIC,1,MOD.RECORD.size)
        full=MOD.RECORD.pack(
            1,0x3ff,1,7,11,101,
            100,110,120,130,140,150,160,170,200,250)
        partial=MOD.RECORD.pack(
            1,0x7,2,7,12,0,
            300,320,350,0,0,0,0,0,0,0)
        final_for_partial=MOD.RECORD.pack(
            1,0x3f8,2,7,12,202,
            0,0,0,360,370,380,390,400,450,500)
        path.write_bytes(header+full+partial+final_for_partial)
        out=MOD.summarize(MOD.read(path))
        assert out["record_count"]==3
        assert out["trace_count"]==2
        assert out["segments"]["frame_receive_to_decode"]["count"]==2
        assert out["segments"]["frame_receive_to_decode"]["p50"] in {10,20}
        assert out["segments"]["arb_decision_to_risk"]["count"]==2
        assert out["segments"]["http_ack_to_user_ws_match"]["count"]==2
        assert out["segments"]["frame_receive_to_user_ws_match"]["max"]==200

if __name__=="__main__":
    test_binary_trace_summary_ignores_missing_stages()
