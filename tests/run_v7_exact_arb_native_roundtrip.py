"""CTest: Python graph/hotset -> proof-verifying C++ loader -> native decisions."""
import hashlib
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).resolve().parents[1]/"scripts")]
from test_v7_exact_arb_hotset_selection import MODEL, _event, _raw_market, _registry, _hotset
from v7_exact_arb_exchange_universe import build_snapshot
from v7_exact_arb_hotset_selection import compile_selection
from v7_unified_exact_arb_graph import compile_graph
from v7_exact_arb_native_evidence import Evidence


def main():
    now = int(datetime(2026, 9, 23, 12, tzinfo=timezone.utc).timestamp()*1000)
    universe = build_snapshot([_event([_raw_market("1")])], MODEL, generated_at_ms=now)
    graph = compile_graph([_registry()], universe, MODEL)
    hotset = {**_hotset(graph, [graph["relations"][0]["relation_id"]]), "timestamp_ms": now}
    selection, _ = compile_selection(graph, universe, hotset, MODEL, 64, as_of_ms=now)
    def run(value):
        return subprocess.run([sys.argv[1], "--stdin"], input=json.dumps(value), text=True,
                              capture_output=True, timeout=10)
    outputs=[]
    for _ in range(3):
        result=run(selection)
        assert result.returncode == 0, result.stderr
        outputs.append(hashlib.sha256(result.stdout.encode()).hexdigest())
        decisions=json.loads(result.stdout)
        assert decisions[0] == {"handle":0,"reject":0,"quantity":5000000,"net":997500}
        assert decisions[1]["reject"] != 0  # no synthetic SELL inventory
    assert len(set(outputs)) == 1
    payload=json.loads(selection["native_runtime_bundle"]["payload"])
    payload["relations"][0]["legs"][0]["payout_vector"][0]=["0","1"]
    wire=json.dumps(payload,sort_keys=True,separators=(",",":"))
    selection["native_runtime_bundle"]={"payload":wire,"sha256":hashlib.sha256(wire.encode()).hexdigest()}
    result=run(selection)
    assert result.returncode != 0 and "native_payoff_proof_failed" in result.stderr
    # Real WS decoder + native bounded writer -> offline episode reducer. The
    # fixture intentionally loses 5,000 observations to overload/disk suppression.
    result = subprocess.run([sys.argv[1], "--emit-evidence"], text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    fixture = json.loads(result.stdout)
    with tempfile.TemporaryDirectory(prefix="native-evidence-roundtrip-") as directory:
        wire = fixture["native_runtime_bundle"]
        Path(directory, wire["sha256"]+".json").write_text(wire["payload"])
        evidence = Evidence(":memory:", directory, "a"*40)
        try:
            for row in fixture["rows"]:
                evidence.ingest(row)
            report = evidence.finish()
            from v7_exact_arb_control_history import ControlHistory
            first=fixture["rows"][0]
            control=ControlHistory(evidence.db,fixture["control_wires"],first["model_sha"],
                first["observer_session_id"],first["session_manifest_sha256"])
            assert control.receipt()["state"]=="CHECKPOINTED_PREFIX"
            assert control.db.execute("SELECT kind FROM control_events WHERE seq=?",
                (first["control_admission_sequence"],)).fetchone()[0]=="ADMIT"
            assert control.db.execute("SELECT selection FROM control_events WHERE seq=?",
                (first["control_admission_sequence"],)).fetchone()[0]==first["selection_receipt_sha256"]
            assert report["engineering_evidence"]["evaluations"] == 2
            assert report["engineering_evidence"]["missing_observation_rows"] == 5000
            counts = report["families"]["SAME_MARKET_BINARY_COMPLETE_SET:BUY"]["evaluation_funnel"]
            assert counts["raw_positive_segments"] == 2
            assert counts["raw_observed_starts"] == 0
            assert report["economic_evidence"]["net_pnl"] is None
        finally:
            evidence.close()


if __name__ == "__main__":
    main()
