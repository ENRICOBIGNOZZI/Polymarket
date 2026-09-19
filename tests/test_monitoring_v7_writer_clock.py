from __future__ import annotations
import json
import os
import sys
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'monitoring'))
import exporter_v7 as exporter


def writer_fixture(root: Path, timestamp_ms=1002550, **overrides):
    control=root/'control';control.mkdir(exist_ok=True)
    (control/'runtime.lock').mkdir(exist_ok=True)
    (control/'runtime.lock/pid').write_text(str(os.getpid()))
    (root/'ledger').mkdir(exist_ok=True)
    value={'schema':'polymarket_v7_ledger_writer_status_v1','model_sha':'a'*40,
      'run_id':'run-id','paper_only':True,'authenticated_execution':False,
      'real_order_submission':False,'healthy':True,'pid':os.getpid(),
      'timestamp_ms':timestamp_ms,'ledger_writable':True,**overrides}
    (control/'ledger_writer_status.json').write_text(json.dumps(value))
    return {'model_sha':'a'*40,'run_id':'run-id','pid':os.getpid()}


def test_fresh_writer_during_slow_snapshot_uses_read_clock(tmp_path,monkeypatch):
    runtime=writer_fixture(tmp_path)
    monkeypatch.setattr(exporter.time,'time',lambda:1002.6)
    # Reproduce the old snapshot-start clock falsely making the writer future-dated.
    assert exporter._operations(tmp_path,runtime,1000)['ledger_writable'] is False
    actual=exporter._operations(tmp_path,runtime,None)
    assert actual['ledger_writable'] is True
    assert actual['writer_observed_at_unix']==1002.6


@pytest.mark.parametrize('stamp',[990000,1005000])
def test_read_clock_does_not_relax_stale_or_future_validation(tmp_path,monkeypatch,stamp):
    runtime=writer_fixture(tmp_path,timestamp_ms=stamp)
    monkeypatch.setattr(exporter.time,'time',lambda:1002.6)
    assert exporter._operations(tmp_path,runtime,None)['ledger_writable'] is False


@pytest.mark.parametrize('overrides',[{'run_id':'other'},{'model_sha':'b'*40},
    {'healthy':False},{'ledger_writable':False},{'authenticated_execution':True},
    {'real_order_submission':True},{'paper_only':False}])
def test_live_clock_keeps_identity_safety_and_actual_writability_checks(tmp_path,monkeypatch,overrides):
    runtime=writer_fixture(tmp_path,**overrides)
    monkeypatch.setattr(exporter.time,'time',lambda:1002.6)
    assert exporter._operations(tmp_path,runtime,None)['ledger_writable'] is False


def test_explicit_clock_remains_deterministic(tmp_path,monkeypatch):
    runtime=writer_fixture(tmp_path)
    monkeypatch.setattr(exporter.time,'time',lambda:999999.0)
    assert exporter._operations(tmp_path,runtime,1002.6)['ledger_writable'] is True
