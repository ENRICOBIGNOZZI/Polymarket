from __future__ import annotations
import json,sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
import v7_probability_evaluation_gate as gate

def model(path:Path,sha:str):
    path.write_text(json.dumps({
        "schema":"v7_probability_logit_candidate_v1","code_sha":sha,
        "paper_only":True,"authenticated_execution":False,"real_order_submission":False,
        "test_duration_seconds":7200}))

def test_restart_reuses_exact_end(tmp_path):
    sha="a"*40;m=tmp_path/"model.json";g=tmp_path/"gate.json";model(m,sha)
    first=gate.prepare(g,m,sha,7200,now_ns=1_000_000_000)
    second=gate.prepare(g,m,sha,7200,now_ns=9_000_000_000)
    assert first==second
    assert first["end_wall_ns"]==1_000_000_000+7200*1_000_000_000

def test_different_model_or_sha_cannot_extend_existing_gate(tmp_path):
    sha="a"*40;m=tmp_path/"model.json";g=tmp_path/"gate.json";model(m,sha)
    gate.prepare(g,m,sha,7200,now_ns=1)
    value=json.loads(m.read_text());value["extra"]="different";m.write_text(json.dumps(value))
    with pytest.raises(ValueError,match="identity_mismatch"): gate.prepare(g,m,sha,7200,now_ns=999)
    model(m,"b"*40)
    with pytest.raises(ValueError): gate.prepare(g,m,"b"*40,7200,now_ns=999)

def test_only_two_hour_duration_is_valid(tmp_path):
    sha="a"*40;m=tmp_path/"model.json";model(m,sha)
    with pytest.raises(ValueError,match="7200"): gate.prepare(tmp_path/"g",m,sha,3600,now_ns=1)

def test_runtime_bundle_contains_gate_and_signal_policy():
    x=json.loads((ROOT/"deploy/london/runtime_manifest.json").read_text())
    assert "scripts/v7_probability_evaluation_gate.py" in x["python_entrypoints"]
    assert "config/v7_crypto_signal_policy.json" in x["support_files"]
