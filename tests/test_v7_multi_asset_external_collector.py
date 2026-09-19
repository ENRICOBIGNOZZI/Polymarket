from __future__ import annotations
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
import v7_multi_asset_external_collector as collector


def registry():
    return json.loads((ROOT/"config/v7_crypto_settlement_markets.json").read_text())


def ready_status(asset: str, sha: str, now_ns: int) -> dict:
    secondary = "bybit_spot" if asset == "BNB" else "coinbase_spot"
    return {
        "schema":"polymarket_v7_external_venue_runtime_v1",
        "asset":asset,"code_sha":sha,"state":"OPERATIONAL","valid":True,
        "paper_only":True,"authenticated_execution":False,"real_order_submission":False,
        "timestamp_ns":now_ns-1_000_000,
        "raw_frame_tapes":{
            "binance_spot":{"enabled":True,"writer_healthy":True,"evidence_valid":True,"written":10,"dropped":0},
            secondary:{"enabled":True,"writer_healthy":True,"evidence_valid":True,"written":5,"dropped":0},
        },
    }


def test_registry_symbols_cover_exactly_six_assets_consistently():
    symbols=collector.canonical_symbols(registry())
    assert set(symbols)==set(collector.ASSETS)
    assert symbols["BTC"]["binance_spot"]=="BTCUSDT"
    assert symbols["BNB"]["binance_spot"]=="BNBUSDT"
    assert symbols["BNB"]["coinbase_spot"]==""
    assert all(row["binance_spot"] for row in symbols.values())


def test_data_ready_requires_primary_and_secondary_tapes_without_drops():
    sha="a"*40; now=10_000_000_000
    for asset in collector.ASSETS:
        ok,reason=collector.data_ready(ready_status(asset,sha,now),asset=asset,sha=sha,now_ns=now)
        assert ok and reason==""
    bad=ready_status("ETH",sha,now)
    bad["raw_frame_tapes"]["binance_spot"]["dropped"]=1
    assert collector.data_ready(bad,asset="ETH",sha=sha,now_ns=now)==(False,"BINANCE_TAPE_NOT_READY")


def test_data_ready_rejects_stale_or_wrong_asset():
    sha="b"*40; now=20_000_000_000
    stale=ready_status("SOL",sha,now)
    stale["timestamp_ns"]=1
    assert collector.data_ready(stale,asset="SOL",sha=sha,now_ns=now)==(False,"STALE")
    wrong=ready_status("XRP",sha,now)
    assert collector.data_ready(wrong,asset="DOGE",sha=sha,now_ns=now)==(False,"STATUS_CONTRACT")


if __name__=="__main__":
    tests=sorted((n,f) for n,f in globals().items() if n.startswith("test_") and callable(f))
    for _,test in tests:test()
    print(f"{len(tests)} function tests passed")


def test_tape_paths_are_restart_unique_for_supervisor_process():
    btc=collector.child_paths(Path("/tmp/run"),"BTC")
    eth=collector.child_paths(Path("/tmp/run"),"ETH")
    assert str(os.getpid()) in btc["tape"].name
    assert str(os.getpid()) in eth["tape"].name
    assert btc["tape"] != eth["tape"]
    assert btc["tape"].parent.name == "tapes"
    assert eth["tape"].parent.name == "tapes"


def test_degraded_composite_with_complete_primary_secondary_tapes_is_collection_ready():
    sha="c"*40; now=30_000_000_000
    row=ready_status("BNB",sha,now)
    row["state"]="WARMING_OR_DEGRADED"
    row["valid"]=False
    ok,reason=collector.data_ready(row,asset="BNB",sha=sha,now_ns=now)
    assert ok and reason==""


def test_degraded_composite_still_fails_when_secondary_tape_missing():
    sha="d"*40; now=40_000_000_000
    row=ready_status("BNB",sha,now)
    row["state"]="WARMING_OR_DEGRADED"
    row["valid"]=False
    row["raw_frame_tapes"]["bybit_spot"]["written"]=0
    assert collector.data_ready(row,asset="BNB",sha=sha,now_ns=now)==(
        False,"SECONDARY_SPOT_TAPE_NOT_READY")


def test_collector_state_degrades_without_process_exit():
    assert collector.collector_state(ready=6,total=6,elapsed=500,startup_timeout=60)=="OPERATIONAL"
    assert collector.collector_state(ready=5,total=6,elapsed=10,startup_timeout=60)=="WARMING"
    assert collector.collector_state(ready=5,total=6,elapsed=61,startup_timeout=60)=="DEGRADED"
    source=(ROOT/"scripts/v7_multi_asset_external_collector.py").read_text()
    assert 'state="BLOCKED_CHILD_EXIT"' not in source
    assert 'return 70' not in source
    assert 'return 77' not in source
    assert 'CHILD_RESTART_FAILED' in source


def test_tape_session_id_changes_across_restarts():
    first=collector.child_paths(Path("/tmp/run"),"BNB",session_id="session-a")
    second=collector.child_paths(Path("/tmp/run"),"BNB",session_id="session-b")
    assert first["tape"] != second["tape"]
    assert first["tape"].name.endswith("session-a.bin")
    assert second["tape"].name.endswith("session-b.bin")


class FakeCaptureProcess:
    def __init__(self, pid=123):
        self.pid, self.returncode = pid, None
        self.signals = []
    def poll(self):
        return self.returncode
    def terminate(self):
        self.signals.append("TERM")
    def kill(self):
        self.signals.append("KILL")
        self.returncode = -9


def capture_fixture(tmp_path):
    from types import SimpleNamespace
    import io
    now_ns = 60_000_000_000
    args = SimpleNamespace(run_root=tmp_path, model_sha="e"*40)
    status = ready_status("BTC", args.model_sha, now_ns)
    path = tmp_path / "status.json"
    path.write_text(json.dumps(status))
    child = collector.Child("BTC", {}, FakeCaptureProcess(), io.BytesIO(), path)
    return args, child, status, now_ns


def test_recovery_preserves_actual_drop_snapshot_before_signal(tmp_path):
    args, child, status, now_ns = capture_fixture(tmp_path)
    status["raw_frame_tapes"]["binance_spot"].update(dropped=159, evidence_valid=False)
    child.status_path.write_text(json.dumps(status))
    assert collector.recover_invalid_capture(args, child, now=60., now_ns=now_ns)=="CAPTURE_RECOVERY_PENDING"
    assert child.process.signals==["TERM"]
    record=json.loads(Path(child.last_capture_incident).read_text())
    assert record['status']['raw_frame_tapes']['binance_spot']['dropped']==159
    assert record['missing_frames_recovered'] is False
    assert record['raw_files_deleted'] is False
    assert record['execution_authority'] is False
    assert json.loads(child.status_path.read_text())==status
    assert child.capture_incident_count==1
    collector.recover_invalid_capture(args, child, now=61., now_ns=now_ns+1_000_000_000)
    assert child.process.signals==["TERM"]
    assert len(list((tmp_path/'external_fair/capture_incidents').glob('*.json')))==1


def test_recovery_never_restarts_normal_warmup_or_policy_suppression(tmp_path):
    args, child, status, now_ns = capture_fixture(tmp_path)
    primary=status['raw_frame_tapes']['binance_spot']
    primary.update(written=0,evidence_valid=False)
    child.status_path.write_text(json.dumps(status))
    assert collector.recover_invalid_capture(args,child,now=60.,now_ns=now_ns)==''
    primary.update(written=10,dropped=1,suppression_active=True)
    child.status_path.write_text(json.dumps(status))
    assert collector.recover_invalid_capture(args,child,now=60.,now_ns=now_ns)==''
    primary['suppression_active']=False;status['disk_pressure']=True
    child.status_path.write_text(json.dumps(status))
    assert collector.recover_invalid_capture(args,child,now=60.,now_ns=now_ns)==''
    assert child.process.signals==[]


def test_cannot_hide_capture_fault_when_incident_storage_fails(tmp_path,monkeypatch):
    import pytest
    args, child, status, now_ns = capture_fixture(tmp_path)
    status['raw_frame_tapes']['binance_spot']['dropped']=1
    child.status_path.write_text(json.dumps(status))
    def fail(*a,**k): raise OSError('disk unavailable')
    monkeypatch.setattr(collector,'preserve_capture_incident',fail)
    with pytest.raises(OSError):
        collector.recover_invalid_capture(args,child,now=60.,now_ns=now_ns)
    assert child.process.signals==[] and child.recovery_requested_at is None


def test_recovery_has_grace_period_and_bounded_rate(tmp_path):
    args, child, status, now_ns=capture_fixture(tmp_path)
    status['raw_frame_tapes']['binance_spot']['dropped']=1
    child.status_path.write_text(json.dumps(status))
    child.recovery_times=[10.,20.,30.]
    assert collector.recover_invalid_capture(args,child,now=60.,now_ns=now_ns)=='CAPTURE_RECOVERY_BUDGET_EXHAUSTED'
    assert child.process.signals==[] and child.capture_incident_count==1
    child.recovery_times=[];child.next_restart_monotonic=61.
    assert collector.recover_invalid_capture(args,child,now=60.,now_ns=now_ns)=='CAPTURE_RECOVERY_BACKOFF'
    assert child.process.signals==[]
    collector.recover_invalid_capture(args,child,now=62.,now_ns=now_ns)
    collector.recover_invalid_capture(args,child,now=69.,now_ns=now_ns)
    assert child.process.signals==['TERM']
    collector.recover_invalid_capture(args,child,now=70.,now_ns=now_ns)
    assert child.process.signals==['TERM','KILL']


def test_dead_or_recovering_child_cannot_count_as_ready(tmp_path,monkeypatch):
    args, child, status, now_ns=capture_fixture(tmp_path)
    monkeypatch.setattr(collector.time,'time_ns',lambda:now_ns)
    child.process.returncode=0
    path=tmp_path/'aggregate.json'
    collector.write_status(path,args=args,children=[child],state='OPERATIONAL',blockers={})
    value=json.loads(path.read_text())
    assert value['ready_assets']==0 and value['state']=='DEGRADED'
    child.process.returncode=None;child.recovery_requested_at=60.
    collector.write_status(path,args=args,children=[child],state='OPERATIONAL',blockers={})
    assert json.loads(path.read_text())['ready_assets']==0


def test_healthy_new_generation_does_not_erase_historical_incident(tmp_path,monkeypatch):
    args, child, status, now_ns=capture_fixture(tmp_path)
    monkeypatch.setattr(collector.time,'time_ns',lambda:now_ns)
    child.capture_incident_count=1;child.last_capture_incident='retained-incident.json'
    collector.write_status(tmp_path/'aggregate.json',args=args,children=[child],state='OPERATIONAL',blockers={})
    value=json.loads((tmp_path/'aggregate.json').read_text())
    assert value['ready_assets']==1
    assert value['historical_capture_gaps_repaired'] is False
    assert value['assets'][0]['capture_incident_count']==1
    assert value['assets'][0]['last_capture_incident']=='retained-incident.json'


def test_wrong_generation_status_never_triggers_recovery(tmp_path):
    args,child,status,now_ns=capture_fixture(tmp_path)
    status['raw_frame_tapes']['binance_spot']['dropped']=1
    status['code_sha']='f'*40
    child.status_path.write_text(json.dumps(status))
    assert collector.recover_invalid_capture(args,child,now=60.,now_ns=now_ns)==''
    assert not child.process.signals
