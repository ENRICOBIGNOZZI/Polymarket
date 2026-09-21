#!/usr/bin/env python3
"""Run the bounded 2H multi-alpha PAPER research program on London evidence."""
from __future__ import annotations

import argparse
import io
import json
import re
import tarfile
from pathlib import Path

from v7_london_ssm_deploy import REGION, run
from v7_direct_action_research_ssm import remote_context, upload, download, extract

SHA = re.compile(r"^[0-9a-f]{40}$")
INSTANCE = re.compile(r"^i-[0-9a-f]+$")
PATHS = (
    "research/walk_forward_v3/__init__.py",
    "research/walk_forward_v3/multi_alpha_2h.py",
    "research/walk_forward_v3/all_crypto_compact_equity.py",
    "research/walk_forward_v3/btc_compact_equity.py",
    "research/walk_forward_v3/direct_action.py",
    "research/walk_forward_v3/dynamic_exit.py",
    "research/walk_forward_v2/__init__.py",
    "research/walk_forward_v2/core.py",
    "research/economic/__init__.py",
    "research/economic/causal_replay.py",
    "scripts/__init__.py",
    "scripts/v7_multi_crypto_compact_pm_tape.py",
    "research/requirements-learning.txt",
)
REQUIRED = (
    "00_executive_summary.md",
    "01_2h_manifest.json",
    "02_data_coverage.json",
    "03_baseline_manifest.json",
    "04_baseline_2h.json",
    "05_external_backfill.json",
    "06_causal_join.json",
    "07_univariate_alpha.json",
    "08_nested_models.json",
    "09_information_latency.json",
    "10_entry_exit_information_cube.json",
    "11_signal_decay.json",
    "12_momentum_reversal.json",
    "13_dynamic_exit.json",
    "14_feature_costs.json",
    "15_equities.json",
    "16_feature_scorecard.json",
    "17_shortlist.json",
    "18_rejected_features.json",
    "19_next_stage_plan.md",
)


def archive(repo: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for rel in PATHS:
            path = repo / rel
            if not path.is_file():
                raise FileNotFoundError(rel)
            tf.add(path, arcname=rel, recursive=False)
    return buf.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--minimum-wall-ns", type=int, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    if not INSTANCE.fullmatch(args.instance_id):
        parser.error("invalid instance")
    if not SHA.fullmatch(args.expected_sha):
        parser.error("exact SHA required")

    repo = Path(__file__).resolve().parents[1]
    context = remote_context(args.instance_id)
    remote = "/tmp/polymarket-multi-alpha-2h-" + args.expected_sha[:12]
    upload(args.instance_id, remote, archive(repo))

    command = f"""set -euo pipefail
rm -rf {remote}/src {remote}/output
mkdir -p {remote}/src {remote}/output
tar -xzf {remote}/source.tgz -C {remote}/src
python3 -m venv {remote}/venv
{remote}/venv/bin/pip install --disable-pip-version-check --quiet -r {remote}/src/research/requirements-learning.txt
PYTHONPATH={remote}/src:{context['app_dir']} nice -n 18 {remote}/venv/bin/python -m research.walk_forward_v3.multi_alpha_2h \
  --root {context['run_root']} \
  --minimum-wall-ns {args.minimum_wall_ns} \
  --code-sha {args.expected_sha} \
  --output-dir {remote}/output/multi-alpha-2h
python3 - {remote}/output/multi-alpha-2h {args.expected_sha} <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1]); sha=sys.argv[2]
required={json.dumps(list(REQUIRED))}
missing=[name for name in required if not (root/name).is_file()]
assert not missing, missing
manifest=json.load(open(root/'01_2h_manifest.json'))
baseline=json.load(open(root/'03_baseline_manifest.json'))
summary=json.load(open(root/'17_shortlist.json'))
assert manifest['paper_only'] is True
assert manifest['authenticated_execution'] is False
assert manifest['real_order_submission'] is False
assert manifest['real_capital_at_risk'] is False
assert manifest['automatic_promotion'] is False
assert manifest['window_end_ns']-manifest['window_start_ns']==7_200_000_000_000
assert manifest['selection']['profitability_used_for_selection'] is False
assert baseline['baseline_code_sha']==sha
assert baseline['latencies_ms']==[5,10,25,50,100,250]
assert baseline['exit_horizons_ms']==[500,750,1000,1500,2000,3000,4000,5000,7500,10000]
assert baseline['size_shares']==5.0
assert len(summary['candidates'])<=4
print('MULTI_ALPHA_2H_READY')
print(json.dumps({
  'window_start_ns':manifest['window_start_ns'],
  'window_end_ns':manifest['window_end_ns'],
  'assets':baseline['assets'],
  'contract_horizons':baseline['contract_horizons'],
  'shortlist':summary['candidates'],
},sort_keys=True))
PY
tar -C {remote}/output/multi-alpha-2h -czf {remote}/results.tgz .
python3 - {remote}/results.tgz <<'PY'
from pathlib import Path
import hashlib,json,sys
p=Path(sys.argv[1])
print('MULTI_ALPHA_2H_RESULT='+json.dumps({
  'bytes':p.stat().st_size,
  'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),
},sort_keys=True))
PY"""

    stdout, _ = run(REGION, args.instance_id, command, 5400)
    marker = next(
        line for line in stdout.splitlines()
        if line.startswith("MULTI_ALPHA_2H_RESULT=")
    )
    info = json.loads(marker.split("=", 1)[1])
    payload = download(args.instance_id, remote, info)
    extract(payload, args.output_dir)
    run(REGION, args.instance_id, f"rm -rf {remote}", 60)

    produced = args.output_dir
    print("multi_alpha_2h_output=" + str(produced))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
