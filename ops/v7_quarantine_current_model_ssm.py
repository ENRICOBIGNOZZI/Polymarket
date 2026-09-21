#!/usr/bin/env python3
"""Quarantine one exact London PAPER model without disturbing safe observers.

If the exact target model is already SAFE_ACTIONS_ONLY with no new-risk
authority, this is a read-only verification. If that exact model unexpectedly
has economic authority, the canonical PAPER service is stopped fail-closed.
A different model SHA is never touched.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys

from v7_london_ssm_deploy import REGION, run

INSTANCE_RE = re.compile(r"^i-[0-9a-f]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def load_request(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "schema", "version", "request_id", "instance_id",
        "expected_model_sha", "output_directory",
        "paper_only", "authenticated_execution",
        "real_order_submission", "real_capital_at_risk",
    }
    if set(value) != required:
        raise ValueError("unexpected quarantine request fields")
    if value["schema"] != "polymarket_v7_current_model_quarantine_request_v1":
        raise ValueError("invalid quarantine request schema")
    if value["version"] != 1:
        raise ValueError("invalid quarantine request version")
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", value["request_id"]):
        raise ValueError("invalid quarantine request id")
    if not INSTANCE_RE.fullmatch(value["instance_id"]):
        raise ValueError("invalid instance id")
    if not SHA_RE.fullmatch(value["expected_model_sha"]):
        raise ValueError("invalid expected model SHA")
    if not re.fullmatch(
        r"docs/research/model-quarantine-[0-9]{4}-[0-9]{2}-[0-9]{2}",
        value["output_directory"],
    ):
        raise ValueError("invalid quarantine output directory")
    if not (
        value["paper_only"] is True
        and value["authenticated_execution"] is False
        and value["real_order_submission"] is False
        and value["real_capital_at_risk"] is False
    ):
        raise ValueError("quarantine request violates PAPER-only boundary")
    return value


def quarantine(instance: str, expected_model_sha: str) -> dict:
    command = r"""set -euo pipefail
python3 - <<'PY'
import json,shlex,subprocess
from pathlib import Path

unit='polymarket-v7-paper.service'
expected='__EXPECTED__'
env=subprocess.check_output(
    ['systemctl','show',unit,'-p','Environment','--value'], text=True)
root=Path(next(
    item.split('=',1)[1] for item in shlex.split(env)
    if item.startswith('PM_V7_RUN_ROOT=')
)).resolve()
state=json.loads((root/'control/runtime_status.json').read_text())

base={
    'schema':'polymarket_v7_current_model_quarantine_receipt_v1',
    'paper_only':True,
    'authenticated_execution':False,
    'real_order_submission':False,
    'real_capital_at_risk':False,
    'expected_model_sha':expected,
    'observed_model_sha':str(state.get('model_sha') or ''),
    'runtime_state_before':state.get('state'),
    'economic_new_risk_ready_before':state.get('economic_new_risk_ready'),
    'economic_decision_state_before':state.get('economic_decision_state'),
    'authorized_alpha_actions_before':state.get('authorized_alpha_actions'),
    'safe_actions_before':state.get('safe_actions'),
}
assert state.get('paper_only') is True
assert state.get('authenticated_execution') is False
assert state.get('real_order_submission') is False
assert state.get('real_capital_at_risk') is False

if base['observed_model_sha'] != expected:
    base.update({
        'state':'MODEL_CHANGED_NO_ACTION',
        'mutation_performed':False,
        'paper_service_stopped':False,
        'reason':'EXPECTED_CURRENT_MODEL_SHA_NO_LONGER_DEPLOYED',
    })
    print('MODEL_QUARANTINE='+json.dumps(base,sort_keys=True))
    raise SystemExit(0)

alpha=state.get('authorized_alpha_actions')
already_safe=(
    state.get('economic_new_risk_ready') is False
    and state.get('economic_decision_state') == 'SAFE_ACTIONS_ONLY'
    and alpha in (None, [])
    and set(state.get('safe_actions') or []) <= {'CANCEL','WITHDRAW','NOTHING'}
)
if already_safe:
    base.update({
        'state':'ALREADY_ECONOMICALLY_QUARANTINED',
        'mutation_performed':False,
        'paper_service_stopped':False,
        'reason':'NO_NEW_RISK_OR_ALPHA_AUTHORITY',
    })
    print('MODEL_QUARANTINE='+json.dumps(base,sort_keys=True))
    raise SystemExit(0)

# Fail closed only for the exact model the operator asked to quarantine.
subprocess.run(['sudo','systemctl','stop',unit],check=True)
active=subprocess.check_output(
    ['systemctl','show',unit,'-p','ActiveState','--value'],text=True).strip()
pid=subprocess.check_output(
    ['systemctl','show',unit,'-p','MainPID','--value'],text=True).strip()
if active not in {'inactive','failed'} or pid != '0':
    raise SystemExit('quarantine_failed_to_stop_exact_unsafe_model')
base.update({
    'state':'STOPPED_EXACT_UNSAFE_CURRENT_MODEL',
    'mutation_performed':True,
    'paper_service_stopped':True,
    'service_active_state_after':active,
    'service_main_pid_after':int(pid),
    'reason':'EXACT_TARGET_MODEL_EXPOSED_NEW_RISK_OR_ALPHA_AUTHORITY',
})
print('MODEL_QUARANTINE='+json.dumps(base,sort_keys=True))
PY
"""
    command = command.replace("__EXPECTED__", expected_model_sha)
    stdout, _ = run(REGION, instance, command, 120)
    line = next(
        item for item in stdout.splitlines()
        if item.startswith("MODEL_QUARANTINE=")
    )
    return json.loads(line.split("=", 1)[1])


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: v7_quarantine_current_model_ssm.py REQUEST")
    request = load_request(Path(sys.argv[1]))
    receipt = quarantine(
        request["instance_id"],
        request["expected_model_sha"],
    )
    destination = Path(request["output_directory"])
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "quarantine.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("MODEL_QUARANTINE_LOCAL_OUTPUT=" + str(destination))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
