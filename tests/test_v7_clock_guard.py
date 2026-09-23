import importlib.util
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location("v7_clock_guard",ROOT/"scripts/v7_clock_guard.py")
MODULE=importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_clock_guard_records_source_failure_without_weakening_fail_closed(tmp_path,monkeypatch):
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
