#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
sys.path.insert(0,str(ROOT/'tests'))
from test_v7_multi_crypto_forward_freeze import draft
from v7_multi_crypto_forward_freeze import freeze
from v7_multi_crypto_readiness_gate import evaluate, valid_frozen_protocol

SHOCK=ROOT/'runs/v7_multi_crypto_shadow/runtime_smoke_5adab31c_20260917/research/shock_calibration_report.json'
RESEARCH=ROOT/'runs/v7_multi_crypto_shadow/runtime_smoke_5adab31c_20260917/research/research_report.json'
LATENCY=ROOT/'docs/v7_multi_crypto/latency_capacity_mechanics_smoke_20260917.json'


def test_current_repository_is_blocked_for_evidence_not_authority() -> None:
    value=evaluate(ROOT,shock_report=SHOCK,research_report=RESEARCH,latency_report=LATENCY)
    assert value['state']=='BLOCKED_EVIDENCE'
    assert value['promotion_authorized'] is False and value['paper_forward_authorized'] is False
    assert 'SHOCK_CALIBRATION_INSUFFICIENT' in value['blockers']
    assert 'SHOCK_TRIGGER_THRESHOLD_NOT_FROZEN' in value['blockers']
    assert 'INDEPENDENT_OOS_REPRICING_EVIDENCE_INSUFFICIENT' in value['blockers']
    assert 'NO_EXECUTABLE_ECONOMIC_PNL_EVIDENCE' in value['blockers']
    assert 'LONDON_AZ_NOT_SELECTED' in value['blockers']
    disk_blocked = value['disk_free_bytes'] < value['disk_required_bytes']
    assert ('DISK_FREE_BELOW_RUNTIME_POLICY' in value['blockers']) is disk_blocked


def test_missing_reports_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        missing=Path(tmp)/'missing.json'
        value=evaluate(ROOT,shock_report=missing,research_report=missing,latency_report=missing)
        assert value['state']=='BLOCKED_EVIDENCE'
        assert 'SHOCK_CALIBRATION_INSUFFICIENT' in value['blockers']
        assert 'INDEPENDENT_OOS_REPRICING_EVIDENCE_INSUFFICIENT' in value['blockers']
        assert 'LATENCY_CAPACITY_MECHANICS_MISSING' in value['blockers']


def test_valid_frozen_protocol_removes_only_protocol_blocker() -> None:
    frozen=freeze(draft())
    assert valid_frozen_protocol(frozen) is True
    tampered=json.loads(json.dumps(frozen)); tampered['frozen_protocol']['entry_policy']['shock_threshold_z']=9.0
    assert valid_frozen_protocol(tampered) is False
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/'forward.json'; path.write_text(json.dumps(frozen))
        value=evaluate(ROOT,shock_report=SHOCK,research_report=RESEARCH,
                       latency_report=LATENCY,forward_protocol=path)
        assert 'MULTI_CRYPTO_FORWARD_PROTOCOL_NOT_FROZEN' not in value['blockers']
        assert value['forward_protocol_valid'] is True
        assert value['state']=='BLOCKED_EVIDENCE'


def test_gate_never_grants_execution_authority() -> None:
    value=evaluate(ROOT,shock_report=SHOCK,research_report=RESEARCH,latency_report=LATENCY)
    assert value['execution_authority'] is False
    assert value['real_order_submission'] is False
    assert value['real_capital_at_risk'] is False


if __name__=='__main__':
    tests=sorted((n,f) for n,f in globals().items() if n.startswith('test_') and callable(f))
    for _,fn in tests: fn()
    print(f'{len(tests)} function tests passed')
