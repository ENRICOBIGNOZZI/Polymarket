from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import sys
import pytest

pytest.importorskip("sklearn")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/"tests"))
from test_v7_cumulative_learning import synthetic_rows, native, label, write
from research.learning import daily
from research.learning.common import canonical, digest


def test_daily_cutoff_receipt_is_idempotent_and_no_new_information(tmp_path):
    root = tmp_path/"private"; root.mkdir(); source = tmp_path/"source"; source.mkdir()
    (root/"research_host.json").write_text(json.dumps({"schema": "v7_research_host_v1",
        "hostname": platform.node(), "role": "RESEARCH_ONLY"}))
    (root/"settings.json").write_text(json.dumps({"source_roots": [str(source)], "sync_london": False}))
    write(source/"observations.jsonl", [native(), label()])
    calls = []
    def train(rows, dataset, output):
        calls.append(len(rows))
        return {"state": "REJECTED", "reasons": ["INSUFFICIENT_DATA"], "report_sha256": "a"*64}
    now = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
    a = daily.run(root, now=now, train_fn=train)
    b = daily.run(root, now=now, train_fn=train)
    assert a == b and calls == [1]
    c = daily.run(root, now=datetime(2026, 9, 21, 12, tzinfo=timezone.utc), train_fn=train)
    assert c["result"] == "NO_NEW_TRAINING_INFORMATION" and calls == [1]
    assert a["automatic_promotion"] is False and a["artifact_sha256"] is None


def test_nested_race_preserves_final_audit_and_is_deterministic(tmp_path, monkeypatch):
    from research.learning import train
    rows = synthetic_rows(360)
    policy = {**train.DEFAULT_POLICY, "ridges": [8.], "weightings": ["market"], "calibrations": ["raw", "platt"]}
    monkeypatch.setattr(train, "FAMILIES", ("pm", "logistic_offset"))
    manifest = {"dataset_sha256": "d"*64, "code_sha": "a"*40, "cutoff_ns": rows[-1]["decision_ns"]+1000}
    a = train.train_stratum(rows, manifest, tmp_path, policy=policy)
    b = train.train_stratum(rows, manifest, tmp_path, policy=policy)
    assert a == b
    assert set(a["forecast_quality"]) == {"pm", "logistic_offset"}
    assert a["final_fit"]["calibration_end_ns"] == a["split"]["audit_start_ns"]
    assert a["state"] == "REJECTED" and "NO_EXECUTABLE_EDGE" in a["reasons"]
    assert a["runtime_artifact_sha256"] is None


def test_native_artifact_exact_identity_and_deterministic_export(tmp_path):
    from research.learning.artifact import export_native, validate
    from research.learning.dataset import native_example
    rows = synthetic_rows(80)
    for r in rows:
        r["stratum"] = "native_causal_features_v1"
        r["native_input"] = native_example(native())["native_input"]
        r["native_input"]["binance_return_100ms_bp"] = 1.+int(r["decision_id"]) % 3
    kwargs = {"dataset": {"dataset_sha256": "d"*64, "code_sha": "a"*40}, "output": tmp_path,
              "fit_cutoff_ns": rows[60]["decision_ns"], "calibration_end_ns": rows[-1]["decision_ns"]+1000}
    sha, a = export_native(rows[:60], rows[60:], **kwargs)
    assert (sha, a) == export_native(rows[:60], rows[60:], **kwargs)
    assert validate(a, "a"*40) == sha
    with pytest.raises(ValueError):
        validate(a, "b"*40)
    for change in ({"paper_only": False}, {"maximum_order_cost_microdollars": 4000000},
                   {"feature_schema": list(reversed(a["feature_schema"]))}):
        with pytest.raises(ValueError):
            validate({**a, **change}, "a"*40)


def test_midnight_scheduler_has_catchup_and_no_promotion():
    timer = (ROOT/"ops/systemd/polymarket-v7-research.timer.in").read_text()
    assert "00:00:00 Europe/Zurich" in timer and "Persistent=true" in timer
    launchd = (ROOT/"ops/launchd/com.polymarket.v7.research-cycle.plist.in").read_text()
    assert "<key>RunAtLoad</key><true/>" in launchd
    script = (ROOT/"research/run_research_cycle.sh").read_text()
    assert "push_runtime_artifacts" not in script and "research.learning.daily" in script


def test_promotion_boolean_gates_cannot_replace_native_oos_evidence(tmp_path):
    from v7_probability_promotion import validate, REQUIRED_GATES
    code='a'*40; dataset='d'*64
    model={'code_sha':code,'training_data_sha256':dataset}
    raw=canonical(model); model_path=tmp_path/'model.json'; model_path.write_bytes(raw)
    sha=digest(raw)
    report={'code_sha':code,'dataset_sha256':dataset,'native_candidate_sha256':sha,
        'state':'PAPER_ELIGIBLE','reasons':[], 'data':{'markets':200,'time_blocks':20},
        'native_runtime_validation':{'code_sha':code,'artifact_sha256':sha,'passed':True},
        'economic_quality':{'portfolio_capital_replay_verified':True,
            'counterfactual_transportability_validated':True}}
    raw=canonical(report); (tmp_path/'report.json').write_bytes(raw)
    proof={'schema':'v7_probability_promotion_proof_v1','code_sha':code,
        'state':'PAPER_ELIGIBLE','paper_only':True,'authenticated_execution':False,
        'real_order_submission':False,'automatic_promotion':False,'dataset_sha256':dataset,
        'artifact_sha256':sha,'report_file':'report.json','report_sha256':digest(raw),
        'gates':{k:True for k in REQUIRED_GATES}}
    path=tmp_path/'proof.json'; path.write_bytes(canonical(proof))
    with pytest.raises(ValueError,match='NATIVE_OOS_METRICS_MISSING'):
        validate(model_path,path,code)
    model_path.write_bytes(canonical({**model,'extra':'modified'}))
    with pytest.raises(ValueError,match='SHA_MISMATCH'):
        validate(model_path,path,code)
