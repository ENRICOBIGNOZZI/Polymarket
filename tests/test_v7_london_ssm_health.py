from __future__ import annotations

import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"ops"))
import v7_london_ssm_health as m

SHA="a"*40


def test_health_command_is_read_only_and_exact_sha():
    command=m.health_command(SHA)
    assert f"SHA={SHA}" in command
    for forbidden in (
        "systemctl stop","systemctl start","systemctl restart",
        "git fetch","git checkout","rm -rf","kill -","apt-get","mv ",
    ):
        assert forbidden not in command
    assert "polymarket_v7_single_writer_ok 1" in command
    assert "polymarket_v7_economic_new_risk_ready 0" in command
    assert "api/dashboards/uid" not in command
    assert "polymarket-v7-multi-crypto" in command
    assert 'query=up{job="polymarket-v7"}' in command
    assert 'if [[ -e "$DEPLOYED_SHA_FILE" ]]' in command
    assert 'cat "$ROOT/control/deployed_sha"' not in command
    assert "V7_SSM_CAPTURE=" in command
    assert "BILATERAL_EXECUTABLE_READY" in command
    assert "native_observations_dropped" in command
    assert "window_seconds':900" in command


def test_health_selects_exact_instance_and_parses_marker(monkeypatch):
    monkeypatch.setattr(m,"aws_json",lambda region,args: {"Account":"123"} if args[:2]==["sts","get-caller-identity"] else {})
    monkeypatch.setattr(m,"candidate_instances",lambda region,stack:["i-123abc"])
    monkeypatch.setattr(m,"probe",lambda region,instances:{
        "i-123abc":{
            "instance_id":"i-123abc","unit_active":True,
            "app":{"user":"ubuntu","path":"/home/ubuntu/polymarket"},
            "app_candidates":[{"user":"ubuntu","path":"/home/ubuntu/polymarket"}],
            "tailscale_ips":[],"paper_only":True,"authenticated_execution":False,
            "real_order_submission":False,
        }
    })
    monkeypatch.setattr(m,"run",lambda *args,**kwargs:(
        'V7_SSM_CAPTURE={"window_seconds":900,"recent_native_rows":7,"recent_kind_counts":{"2":1,"6":6},"pair_state_by_kind":{"2":{"BILATERAL_EXECUTABLE_READY":1},"6":{"BILATERAL_EXECUTABLE_READY":6}},"native_observations_dropped":0}\n'
        'V7_SSM_HEALTH={"sha":"'+SHA+'","core_runtime_healthy":true,"full_data_health_ok":false,"full_data_health_reasons":["warming"],"paper_only":true,"authenticated_execution":false,"real_order_submission":false}\n',
        ""
    ))
    receipt=m.health("eu-west-2",m.STACK,SHA,"","i-123abc")
    assert receipt["runtime"]["core_runtime_healthy"] is True
    assert receipt["runtime"]["full_data_health_ok"] is False
    assert receipt["selected"]["instance_id"]=="i-123abc"
    capture=receipt["runtime"]["bilateral_capture"]
    assert capture["recent_kind_counts"]["2"]==1
    assert capture["pair_state_by_kind"]["2"]["BILATERAL_EXECUTABLE_READY"]==1
    assert capture["native_observations_dropped"]==0
