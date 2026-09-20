#!/usr/bin/env python3
"""Read-only health verification for the canonical London PAPER runtime via AWS SSM."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
from typing import Any

from v7_london_ssm_deploy import (
    REGION,
    STACK,
    SsmDeployError,
    candidate_instances,
    exact_sha,
    parse_marker,
    probe,
    run,
    select_target,
)

SCHEMA = "polymarket_v7_ssm_health_receipt_v1"


def _safe_run_root(selected: dict[str, Any]) -> tuple[str, str]:
    app = selected.get("app") or {}
    user = app.get("user")
    path = app.get("path")
    if not isinstance(user, str) or path != f"/home/{user}/polymarket":
        raise SsmDeployError("selected repository identity invalid")
    run_root = selected.get("run_root")
    if run_root in (None, ""):
        run_root = (
            "/mnt/polymarket-data/paper_v7_london"
            if user == "ubuntu"
            else f"/home/{user}/polymarket-runs/paper_v7_london"
        )
    if not isinstance(run_root, str) or not run_root.startswith("/"):
        raise SsmDeployError("unsafe run root")
    if not (
        run_root.startswith(f"/home/{user}/")
        or run_root.startswith("/mnt/polymarket-data/")
    ):
        raise SsmDeployError("unsafe run root")
    return user, run_root


def health_command(expected_sha: str, selected: dict[str, Any]) -> str:
    if not exact_sha(expected_sha):
        raise SsmDeployError("invalid health SHA")
    user, run_root = _safe_run_root(selected)
    return f"""set -euo pipefail
ROOT={shlex.quote(run_root)}
SHA={expected_sha}
USER_NAME={shlex.quote(user)}
systemctl is-active --quiet polymarket-v7-paper.service
systemctl is-active --quiet polymarket-v7-exporter.service
python3 - "$ROOT" "$SHA" <<'PY'
import csv,json,os,sys,time
from pathlib import Path
root=Path(sys.argv[1]); sha=sys.argv[2]; now=int(time.time())
runtime=json.loads((root/'control/runtime_status.json').read_text(encoding='utf-8'))
identity=json.loads((root/'control/runtime_identity.json').read_text(encoding='utf-8'))
portfolio=json.loads((root/'control/portfolio_state.json').read_text(encoding='utf-8'))
allocation=json.loads((root/'control/allocations/manifest.json').read_text(encoding='utf-8'))
economics=json.loads((root/'canonical_economics.json').read_text(encoding='utf-8'))
universe=json.loads((root/'universe/status.json').read_text(encoding='utf-8'))
assert runtime.get('version') == 7 and runtime.get('model_sha') == sha
assert runtime.get('state') == 'running' and runtime.get('killed') is False
assert runtime.get('paper_only') is True
assert runtime.get('authenticated_execution') is False
assert runtime.get('real_order_submission') is False
assert set(runtime.get('economic_engines') or []) == {{'CRYPTO_SETTLEMENT_ENGINE'}}
assert runtime.get('economic_new_risk_ready') is False
pid=int(runtime.get('pid') or 0); assert pid > 0; os.kill(pid,0)
age=now-int(runtime.get('timestamp') or 0); assert 0 <= age <= 60
assert identity.get('schema') == 'polymarket_v7_runtime_identity_v1'
assert identity.get('verified') is True and identity.get('runtime_sha') == sha
for key in ('config_hash','policy_hash','model_hash','run_id','ledger_id','server_id'):
    assert identity.get(key) == runtime.get(key) and str(identity.get(key) or '')
assert portfolio.get('schema') == 'polymarket_v7_portfolio_guard_v2'
assert set(portfolio.get('engines') or {{}}) == {{'CRYPTO_SETTLEMENT_ENGINE'}}
assert portfolio.get('paper_only') is True and portfolio.get('authenticated_execution') is False
assert portfolio.get('killed') is False and float(portfolio.get('drawdown',1)) < .15
assert now-int(portfolio.get('timestamp') or 0) <= 60
assert allocation.get('schema') == 'polymarket_v7_capital_allocation_v3'
assert set(allocation.get('engine_budgets') or {{}}) == {{'CRYPTO_SETTLEMENT_ENGINE'}}
assert allocation.get('engine_count') == 1
assert economics.get('paper_only') is True
assert economics.get('authenticated_execution') is False
assert economics.get('expected_model_sha') == sha
assert universe.get('schema') == 'polymarket_v7_crypto_universe_status_v1'
assert universe.get('model_sha') == sha and universe.get('state') == 'OPERATIONAL'
assert universe.get('paper_only') is True and universe.get('authenticated_execution') is False
eligible=int(universe.get('eligible_markets') or 0); assert eligible > 0
with (root/'trade_tape.csv').open(newline='',encoding='utf-8') as handle:
    rows=list(csv.DictReader(handle))
assert rows and max(int(float(r.get('received_ms') or 0)) for r in rows) > 0
print('V7_SSM_HEALTH='+json.dumps({{
  'schema':'polymarket_v7_ssm_health_marker_v1',
  'sha':sha,'run_root':str(root),'pid':pid,'runtime_age_s':age,
  'eligible_markets':eligible,'paper_only':True,
  'authenticated_execution':False,'real_order_submission':False
}},sort_keys=True,separators=(',',':')))
PY
curl -fsS http://127.0.0.1:9108/healthz >/dev/null
metrics="$(curl -fsS http://127.0.0.1:9108/metrics)"
grep -q '^polymarket_v7_health 1$' <<<"$metrics"
grep -q '^polymarket_v7_execution_alive 1$' <<<"$metrics"
grep -q '^polymarket_v7_single_writer_ok 1$' <<<"$metrics"
grep -q '^polymarket_v7_exact_sha_ok 1$' <<<"$metrics"
grep -q '^polymarket_v7_paper_only_contract_ok 1$' <<<"$metrics"
grep -q '^polymarket_v7_authenticated_execution_disabled 1$' <<<"$metrics"
grep -q '^polymarket_v7_ledger_valid 1$' <<<"$metrics"
curl -fsS http://127.0.0.1:9090/-/ready >/dev/null
curl -fsS http://127.0.0.1:3000/api/health >/dev/null
curl -fsS http://127.0.0.1:3000/api/dashboards/uid/polymarket-v7 >/dev/null
curl -fsS --get --data-urlencode 'query=up{{job="polymarket-v7"}}'   http://127.0.0.1:9090/api/v1/query |   python3 -c 'import json,sys; r=json.load(sys.stdin).get("data",{{}}).get("result",[]); assert len(r)==1 and r[0]["value"][1]=="1"'
"""


def check(region: str, stack_name: str, expected_sha: str,
          expected_tailscale_ip: str) -> dict[str, Any]:
    if region != REGION or not exact_sha(expected_sha):
        raise SsmDeployError("eu-west-2 and exact SHA required")
    candidates = candidate_instances(region, stack_name)
    probes = probe(region, candidates)
    selected = select_target(probes, expected_tailscale_ip)
    if selected.get("unit_active") is not True:
        raise SsmDeployError("selected London PAPER service is not active")
    stdout, stderr = run(
        region, selected["instance_id"],
        health_command(expected_sha, selected),
        300,
    )
    marker = parse_marker(stdout, "V7_SSM_HEALTH=")
    if marker.get("sha") != expected_sha:
        raise SsmDeployError("health marker SHA mismatch")
    return {
        "schema": SCHEMA,
        "expected_sha": expected_sha,
        "region": region,
        "selected": selected,
        "probes": probes,
        "health": marker,
        "stderr_tail": stderr[-2000:],
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--expected-sha", required=True)
    p.add_argument("--expected-tailscale-ip", default="")
    p.add_argument("--region", default=REGION)
    p.add_argument("--stack-name", default=STACK)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    try:
        receipt = check(
            a.region, a.stack_name, a.expected_sha, a.expected_tailscale_ip,
        )
    except (OSError, ValueError, SsmDeployError) as exc:
        p.exit(2, f"v7_london_ssm_health: {exc}\n")
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(
        json.dumps(receipt, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print("ssm_health_result=success")
    print(f"health_sha={receipt['expected_sha']}")
    print(f"ssm_instance={receipt['selected']['instance_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
