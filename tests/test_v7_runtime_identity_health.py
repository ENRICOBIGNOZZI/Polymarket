from copy import deepcopy
import json
from pathlib import Path
import sys
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/"ops"),str(ROOT/"scripts")]
from v7_runtime_identity import resolve, read_receipt, SAFETY
from v7_runtime_health import collect
from v7_tape_health import sample_tape,tape_growth

SHA="a"*40


def target():
    return json.loads((ROOT/"deploy/london/runtime_identity.json").read_text())


def probes():
    t=target();instance=t["instance_id"]
    return {instance:{"instance_id":instance,"app":{"user":"ubuntu","path":"/home/ubuntu/polymarket"},
        "unit_active":True,"runtime_sha":SHA,"release_sha":SHA,"run_root":"/mnt/polymarket-data/paper_v7_london",
        "tailscale_ips":["100.100.1.1"],"availability_zone":"eu-west-2a",**SAFETY}}


def test_canonical_instance_and_observed_release_not_repository_sha():
    out=resolve(target(),probes(),now_ms=100)
    assert out["runtime_model_sha"]==out["runtime_release_sha"]==SHA
    with pytest.raises(ValueError,match="expected_sha"):resolve(target(),probes(),"b"*40)


@pytest.mark.parametrize("field,value",[("release_sha","b"*40),("runtime_sha",None),
    ("paper_only",False),("run_root","/tmp/other")])
def test_bad_observed_identity_fails_closed(field,value):
    p=probes();p[target()["instance_id"]][field]=value
    with pytest.raises(ValueError):resolve(target(),p)


def test_no_fallback_to_other_active_instance():
    from v7_london_ssm_deploy import SsmDeployError
    p=probes();v=p.pop(target()["instance_id"]);v["instance_id"]="i-"+"f"*17;p[v["instance_id"]]=v
    with pytest.raises(SsmDeployError):resolve(target(),p)


def test_tailnet_constraint_and_receipt_expiry(tmp_path):
    t=target();t["tailscale_ip"]="100.100.2.2"
    with pytest.raises(ValueError,match="tailnet"):resolve(t,probes())
    receipt=tmp_path/"identity.json";receipt.write_text(json.dumps(resolve(target(),probes(),now_ms=100)))
    assert read_receipt(receipt,now_ms=101)["runtime_model_sha"]==SHA
    with pytest.raises(ValueError,match="stale"):read_receipt(receipt,now_ms=300101)


def test_rotation_counts_old_inode_and_new_current(tmp_path):
    p=tmp_path/"current.jsonl";p.write_text('{"n":1}\n')
    a=sample_tape(p,generation="writer",rows_written=1)
    with p.open("a") as f:f.write('{"n":2}\n')
    p.rename(tmp_path/"writer.segment-1000000.jsonl");p.write_text('{"n":3}\n')
    b=sample_tape(p,generation="writer",rows_written=3)
    out=tape_growth(a,b)
    assert out["live"] and out["rotated"] and out["exact_rows"]
    assert out["rows_added"]==2 and out["bytes_added_lower_bound"]==16


def test_retention_missing_counter_and_restart_are_not_zero(tmp_path):
    p=tmp_path/"current.jsonl";p.write_text("a"*100)
    a=sample_tape(p,generation="old")
    p.rename(tmp_path/"writer.segment-1.jsonl");p.write_text("b")
    (tmp_path/"writer.segment-1.jsonl").unlink()
    b=sample_tape(p,generation="old")
    out=tape_growth(a,b)
    assert out["state"]=="RETENTION_GAP" and out["rows_added"] is None
    assert out["bytes_added_lower_bound"]>=0
    b["generation"]="new"
    assert tape_growth(a,b)["live"] is None


def test_truncation_and_counter_regression_fail_closed(tmp_path):
    p=tmp_path/"current.jsonl";p.write_text("x"*100)
    a=sample_tape(p,generation="s",rows_written=10)
    p.write_text("y")
    assert tape_growth(a,sample_tape(p,generation="s",rows_written=11))["live"] is None
    p.write_text("x"*100)
    assert tape_growth(a,sample_tape(p,generation="s",rows_written=9))["state"]=="WRITER_COUNTER_REGRESSION"


def test_shared_directory_cannot_create_false_tape_growth(tmp_path):
    p=tmp_path/"trades.jsonl";p.write_text("a\n")
    a=sample_tape(p)
    (tmp_path/"neighbour.jsonl.segment-000001.jsonl").write_text("unrelated\n")
    assert tape_growth(a,sample_tape(p))["live"] is False
    p.rename(tmp_path/"trades.jsonl.segment-000001.jsonl");p.write_text("b\n")
    assert tape_growth(a,sample_tape(p))["bytes_added_lower_bound"]==2


def test_rewritten_small_file_is_not_mistaken_for_append(tmp_path):
    p=tmp_path/"current.jsonl";p.write_text("a")
    a=sample_tape(p)
    p.write_text("different longer record\n")
    assert tape_growth(a,sample_tape(p))["state"]=="TRUNCATION_OR_INODE_REUSE"


def test_receipt_preserves_missing_fields_and_separates_economics(tmp_path):
    identity=resolve(target(),probes(),now_ms=100)
    out=collect(tmp_path,identity,service_active=True,kill_exists=False,prometheus_ready=True,grafana_ready=True,now_ms=100)
    assert out["book_rows_written_total"] is None
    assert out["native_observations_dropped"] is None
    assert out["engineering_health"]=="UNSAFE_OR_INCOMPLETE"
    assert out["economic_evidence"]=="NOT_ASSESSED_BY_HEALTH_RECEIPT"


def test_complete_health_then_stale_wrong_sha_or_instance_fails(tmp_path):
    identity=resolve(target(),probes(),now_ms=100)
    fixtures={
        "control/runtime_status.json":{"state":"running",**SAFETY},
        "control/native_engine_manager_status.json":{"state":"RUNNING","active_worker_count":30,
            "target_context_count":30,"native_observations_dropped":0,"native_observations_queue_depth":0,
            "native_observations_written":10},
        "control/clock_guard.json":{"state":"OK","safe":True,"offset_ms":0},
        "control/fencing_supervisor_status.json":{"state":"OK","safe":True},
        "research/repricing_book/fillability_ws_status.json":{"state":"running","observer_session_id":"s","book_events_written":10},
        "research/repricing_book/pure_arb_status.json":{"state":"running","cycles_total":0},
    }
    for name,value in fixtures.items():
        p=tmp_path/name;p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(json.dumps({**value,"model_sha":SHA,"timestamp_ms":100}))
    kwargs=dict(service_active=True,kill_exists=False,prometheus_ready=True,grafana_ready=True)
    assert collect(tmp_path,identity,now_ms=101,**kwargs)["engineering_health"]=="HEALTHY"
    assert collect(tmp_path,identity,now_ms=180101,**kwargs)["engineering_health"]!="HEALTHY"
    identity["target_instance_id"]="i-"+"b"*17
    assert collect(tmp_path,identity,now_ms=101,**kwargs)["checks"]["instance_match"] is False
    p=tmp_path/"control/clock_guard.json";v=json.loads(p.read_text());v["model_sha"]="b"*40;p.write_text(json.dumps(v))
    assert collect(tmp_path,identity,now_ms=101,**kwargs)["checks"]["clock_model_match"] is False


def test_scheduled_health_has_no_historical_host_or_request_sha_fallback():
    for name in ("v7-paper-server-health.yml","v7-collection-health.yml","v7-pure-arb-live-probe.yml","v7-pure-arb-evidence-probe.yml"):
        text=(ROOT/".github/workflows"/name).read_text()
        assert "i-0fba2bac9fdc5cbeb" not in text and "i-04042ca7da7a23215" not in text
        assert "ops/v7_runtime_identity.py" in text
    text=(ROOT/".github/workflows/v7-paper-server-health.yml").read_text()
    assert "github.sha" not in text and "git checkout" not in text and "current-deploy-request" not in text


def test_workflow_shell_blocks_parse():
    import subprocess,yaml
    for name in ("v7-paper-server-health.yml","v7-collection-health.yml","v7-pure-arb-evidence-probe.yml"):
        data=yaml.safe_load((ROOT/".github/workflows"/name).read_text())
        for job in data["jobs"].values():
            for step in job["steps"]:
                if "run" in step:
                    subprocess.run(["bash","-n"],input=step["run"],text=True,check=True,capture_output=True)
