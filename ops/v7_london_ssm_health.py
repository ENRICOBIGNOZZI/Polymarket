#!/usr/bin/env python3
"""Read-only exact-SHA health verification for the London PAPER runtime over AWS SSM."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex
from typing import Any

from v7_london_ssm_deploy import (
    REGION, STACK, SsmDeployError, aws_json, candidate_instances,
    exact_sha, parse_marker, probe, run, select_target,
)

SCHEMA = "polymarket_v7_ssm_health_receipt_v1"


def health_command(expected_sha: str) -> str:
    if not exact_sha(expected_sha):
        raise SsmDeployError("invalid health SHA")
    template = r"""set -euo pipefail
trap 'rc=$?; echo "V7_SSM_HEALTH_FAIL line=${LINENO} command=${BASH_COMMAND} rc=${rc}" >&2; exit "$rc"' ERR
SHA=__EXPECTED_SHA__
UNIT=polymarket-v7-paper.service
EXPORTER=polymarket-v7-exporter.service

echo "health_probe=services"
[[ "$(systemctl is-active "$UNIT")" == active ]]
[[ "$(systemctl is-active "$EXPORTER")" == active ]]
APP="$(systemctl show "$UNIT" -p WorkingDirectory --value)"
ENV_RAW="$(systemctl show "$UNIT" -p Environment --value)"
ROOT="$(python3 -c 'import shlex,sys; x=shlex.split(sys.stdin.read()); print(next((v.split("=",1)[1] for v in x if v.startswith("PM_V7_RUN_ROOT=")), ""))' <<<"$ENV_RAW")"
[[ -n "$APP" && -d "$APP" && -n "$ROOT" && -d "$ROOT" ]]
[[ "$(cat "$APP/deploy/london/runtime_sha")" == "$SHA" ]]
DEPLOYED_SHA_FILE="$ROOT/control/deployed_sha"
if [[ -e "$DEPLOYED_SHA_FILE" ]]; then
  [[ -f "$DEPLOYED_SHA_FILE" && ! -L "$DEPLOYED_SHA_FILE" ]]
  [[ "$(cat "$DEPLOYED_SHA_FILE")" == "$SHA" ]]
fi

echo "health_probe=runtime"
python3 - "$ROOT" "$SHA" <<'PY'
import json,os,sys,time
from pathlib import Path
root=Path(sys.argv[1]); sha=sys.argv[2]; now=int(time.time())
r=json.loads((root/'control/runtime_status.json').read_text())
a=json.loads((root/'control/allocations/manifest.json').read_text())
assert r.get('state')=='running', r
assert r.get('model_sha')==sha, r
assert r.get('paper_only') is True, r
assert r.get('authenticated_execution') is False, r
assert r.get('real_order_submission') is False, r
assert r.get('economic_new_risk_ready') is False, r
assert r.get('authorized_alpha_actions') in (None, []), r
assert set(r.get('economic_engines') or [])=={'CRYPTO_SETTLEMENT_ENGINE'}, r
assert a.get('engine_count')==1, a
assert set((a.get('engine_budgets') or {}).keys())=={'CRYPTO_SETTLEMENT_ENGINE'}, a
pid=int(r.get('pid') or 0); assert pid>0, r; os.kill(pid,0)
assert now-int(r.get('timestamp') or 0)<=180, r
PY

echo "health_probe=metrics"
metrics="$(curl -fsS http://127.0.0.1:9108/metrics)"
for expected in   'polymarket_v7_execution_alive 1'   'polymarket_v7_single_writer_ok 1'   'polymarket_v7_exact_sha_ok 1'   'polymarket_v7_paper_only_contract_ok 1'   'polymarket_v7_authenticated_execution_disabled 1'   'polymarket_v7_live_algorithm_count 1'   'polymarket_v7_native_engine_mode 1'   'polymarket_v7_economic_new_risk_ready 0'; do
  if ! grep -Fxq "$expected" <<<"$metrics"; then
    echo "missing_metric_line=$expected" >&2
    exit 66
  fi
done
for metric in   polymarket_execution_opportunities   polymarket_execution_orders_submitted   polymarket_execution_fills   polymarket_execution_complete_fills   polymarket_execution_final_pnl_usd   polymarket_runtime_pnl_usd   polymarket_v7_canonical_submitted_units   polymarket_v7_canonical_complete_units; do
  if ! grep -Eq "^${metric}(\\{| )" <<<"$metrics"; then
    echo "missing_metric=$metric" >&2
    exit 67
  fi
done

echo "health_probe=prometheus"
curl -fsS http://127.0.0.1:9090/-/ready >/dev/null
curl -fsS --get --data-urlencode 'query=up{job="polymarket-v7"}' \
  http://127.0.0.1:9090/api/v1/query | python3 -c 'import json,sys; v=json.load(sys.stdin); r=v.get("data",{}).get("result",[]); assert len(r)==1 and r[0]["value"][1]=="1", v'

echo "health_probe=grafana"
curl -fsS http://127.0.0.1:3000/api/health >/dev/null
curl -fsS http://127.0.0.1:3000/api/dashboards/uid/polymarket-v7 >/dev/null

echo "health_probe=full_health"
full_health="$(curl -sS http://127.0.0.1:9108/healthz 2>/dev/null || true)"
python3 - "$SHA" "$APP" "$ROOT" "$full_health" <<'PY'
import json,sys
sha,app,root,full=sys.argv[1:]
try:
    h=json.loads(full) if full else {}
except json.JSONDecodeError:
    h={}
print('V7_SSM_HEALTH='+json.dumps({
  'sha':sha,'app':app,'run_root':root,
  'paper_only':True,'authenticated_execution':False,'real_order_submission':False,
  'core_runtime_healthy':True,
  'full_data_health_ok':bool(h.get('ok')),
  'full_data_health_reasons':h.get('reasons') if isinstance(h.get('reasons'),list) else [],
  'prometheus_ready':True,'grafana_local_ready':True,
},sort_keys=True,separators=(',',':')))
PY
"""
    return template.replace("__EXPECTED_SHA__", expected_sha)


def health(region: str, stack_name: str, expected_sha: str,
           expected_tailscale_ip: str, expected_instance_id: str) -> dict[str, Any]:
    if region != REGION or not exact_sha(expected_sha):
        raise SsmDeployError("eu-west-2 and exact SHA required")
    identity = aws_json(region, ["sts", "get-caller-identity"])
    candidates = candidate_instances(region, stack_name)
    probes = probe(region, candidates)
    selected = select_target(probes, expected_tailscale_ip, expected_instance_id)
    stdout, stderr = run(region, selected["instance_id"], health_command(expected_sha), 180)
    runtime = parse_marker(stdout, "V7_SSM_HEALTH=")
    if runtime.get("sha") != expected_sha or runtime.get("core_runtime_healthy") is not True:
        raise SsmDeployError("London health receipt mismatch")
    return {
        "schema": SCHEMA,
        "expected_sha": expected_sha,
        "region": region,
        "aws_account": identity.get("Account"),
        "selected": selected,
        "runtime": runtime,
        "stderr_tail": stderr[-2000:],
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
    }


def main() -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--expected-sha",required=True)
    p.add_argument("--expected-tailscale-ip",default="")
    p.add_argument("--expected-instance-id",default="")
    p.add_argument("--region",default=REGION)
    p.add_argument("--stack-name",default=STACK)
    p.add_argument("--output",type=Path,required=True)
    a=p.parse_args()
    try:
        receipt=health(a.region,a.stack_name,a.expected_sha,a.expected_tailscale_ip,a.expected_instance_id)
    except (OSError,ValueError,SsmDeployError) as exc:
        p.exit(2,f"v7_london_ssm_health: {exc}\n")
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(receipt,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    print("ssm_health_result=success")
    print("healthy_sha="+receipt["expected_sha"])
    print("full_data_health_ok="+str(receipt["runtime"]["full_data_health_ok"]).lower())
    return 0


if __name__=="__main__":
    raise SystemExit(main())
