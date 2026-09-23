import importlib.util
import io
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location("v7_clock_guard",ROOT/"scripts/v7_clock_guard.py")
MODULE=importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class _Response:
    status=200
    def __enter__(self): return self
    def __exit__(self,*_): return False
    def read(self): return b"1790165000"


def test_server_time_uses_clob_api_headers(monkeypatch):
    seen={}
    def fake_open(request,timeout):
        seen["request"]=request
        seen["timeout"]=timeout
        return _Response()
    monkeypatch.setattr(MODULE.urllib.request,"urlopen",fake_open)
    monkeypatch.setattr(MODULE.time,"time",lambda:1790165000.0)
    offset,rtt=MODULE.server_time("https://clob.polymarket.com",2.0)
    request=seen["request"]
    headers={k.lower():v for k,v in request.header_items()}
    assert request.full_url=="https://clob.polymarket.com/time"
    assert headers["user-agent"]=="polymarket-v7-clock-guard"
    assert headers["accept"]=="*/*"
    assert headers["content-type"]=="application/json"
    assert headers["connection"]=="close"
    assert seen["timeout"]==2.0
    assert offset==0.0
    assert rtt==0.0


def test_clock_guard_records_source_failure_and_stays_fail_closed(tmp_path,monkeypatch):
    def fail(*_args,**_kwargs):
        raise RuntimeError("clock-probe-failure")
    monkeypatch.setattr(MODULE,"server_time",fail)
    output=tmp_path/"clock.json"
    monkeypatch.setattr(sys,"argv",[
        "v7_clock_guard.py",
        "--model-sha","a"*40,
        "--output",str(output),
        "--fail-after-consecutive-unsafe","1",
        "--interval-seconds","0.01",
    ])
    assert MODULE.main()==2
    value=json.loads(output.read_text())
    assert value["state"]=="CLOCK_SOURCE_UNAVAILABLE"
    assert value["safe"] is False
    assert value["error_type"]=="RuntimeError"
    assert value["error"]=="clock-probe-failure"
