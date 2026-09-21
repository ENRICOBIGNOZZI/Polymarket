#!/usr/bin/env python3
"""Read-only London runner for historical canonical PAPER Maker toxicity research."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import re
import sys
import tarfile

from v7_direct_action_research_ssm import (
    INSTANCE_RE, REGION, download, extract, remote_context, run, upload,
)

SCHEMA="polymarket_v7_maker_historical_research_ssm_request_v1"
SOURCE_PATHS=(
    "scripts/v7_maker_fill_conditioned_toxicity.py",
)


def load_request(path: Path) -> dict:
    value=json.loads(path.read_text(encoding="utf-8"))
    required={
        "schema","version","request_id","instance_id","markout_horizon",
        "minimum_clusters","output_directory","taker_report_path",
        "taker_report_blob_sha","paper_only","authenticated_execution",
        "real_order_submission","real_capital_at_risk",
    }
    if set(value)!=required:
        raise ValueError("unexpected maker research request fields")
    if value["schema"]!=SCHEMA or value["version"]!=1:
        raise ValueError("invalid maker research request schema/version")
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}",value["request_id"]):
        raise ValueError("invalid request id")
    if not INSTANCE_RE.fullmatch(value["instance_id"]):
        raise ValueError("invalid instance")
    if value["markout_horizon"] not in ("250ms","1s","5s"):
        raise ValueError("invalid markout horizon")
    if not isinstance(value["minimum_clusters"],int) or not 3<=value["minimum_clusters"]<=100:
        raise ValueError("invalid minimum clusters")
    if not re.fullmatch(
        r"docs/research/maker-historical-[0-9]{4}-[0-9]{2}-[0-9]{2}",
        value["output_directory"],
    ):
        raise ValueError("invalid output directory")
    if value["taker_report_path"]!="docs/research/direct-action-value-2026-09-21/direct_action.json":
        raise ValueError("unexpected taker report path")
    if not re.fullmatch(r"[0-9a-f]{40}",value["taker_report_blob_sha"]):
        raise ValueError("invalid taker report blob SHA")
    if not (
        value["paper_only"] is True
        and value["authenticated_execution"] is False
        and value["real_order_submission"] is False
        and value["real_capital_at_risk"] is False
    ):
        raise ValueError("PAPER-only authority contract violated")
    return value


def source_archive(repo: Path) -> bytes:
    buffer=io.BytesIO()
    with tarfile.open(fileobj=buffer,mode="w:gz") as archive:
        for relative in SOURCE_PATHS:
            path=repo/relative
            if not path.is_file():
                raise FileNotFoundError(relative)
            archive.add(path,arcname=relative,recursive=False)
    return buffer.getvalue()


def execute(instance: str, remote: str, context: dict, request: dict) -> dict:
    run_root=context["run_root"]
    command=f"""set -euo pipefail
cd {remote}
mkdir -p src output
tar -xzf source.tgz -C src
ledger={run_root}/ledger/execution.jsonl
test -s "$ledger"
python3 src/scripts/v7_maker_fill_conditioned_toxicity.py \
  --maker-evidence "$ledger" \
  --output {remote}/output/maker_toxicity.json \
  --model-output {remote}/output/maker_toxicity_model.json \
  --markout-horizon {request['markout_horizon']} \
  --minimum-clusters {request['minimum_clusters']} \
  --bootstrap-samples 5000
python3 - "$ledger" {remote}/output/maker_toxicity.json {remote}/output/maker_history_receipt.json <<'PY'
import hashlib,json,sys
from collections import Counter
from pathlib import Path
ledger=Path(sys.argv[1]); report_path=Path(sys.argv[2]); output=Path(sys.argv[3])
report=json.loads(report_path.read_text())
counts=Counter()
maker_rows=0
with ledger.open(encoding="utf-8",errors="replace") as stream:
    for line in stream:
        try:
            row=json.loads(line)
        except json.JSONDecodeError:
            continue
        meta=row.get("metadata") if isinstance(row.get("metadata"),dict) else {{}}
        if meta.get("component")!="professional_maker":
            continue
        maker_rows+=1
        sha=str(row.get("model_sha") or "")
        if len(sha)==40:
            counts[sha]+=1
h=hashlib.sha256()
with ledger.open("rb") as stream:
    for block in iter(lambda:stream.read(1<<20),b""):
        h.update(block)
receipt={{
  "schema":"polymarket_v7_maker_historical_research_receipt_v1",
  "paper_only":True,
  "authenticated_execution":False,
  "real_order_submission":False,
  "real_capital_at_risk":False,
  "automatic_promotion":False,
  "evidence_semantics":"CANONICAL_LEDGER_HISTORICAL_MAKER_ROWS_ALL_MODEL_SHAS",
  "ledger_sha256":h.hexdigest(),
  "maker_ledger_rows":maker_rows,
  "maker_rows_by_model_sha":dict(sorted(counts.items())),
  "toxicity_state":report.get("state"),
  "eligible_fill_rows":report.get("eligible_fill_rows"),
  "independent_fill_clusters":report.get("independent_fill_clusters"),
  "markout_horizon":report.get("markout_horizon"),
  "promotion_gate":report.get("promotion_gate"),
}}
output.write_text(json.dumps(receipt,sort_keys=True,indent=2)+"\n")
PY
tar -C {remote}/output -czf {remote}/results.tgz .
python3 - <<'PY'
import hashlib,json
from pathlib import Path
p=Path({remote!r})/"results.tgz"
print("MAKER_HISTORICAL_RESULT="+json.dumps({{
  "bytes":p.stat().st_size,
  "sha256":hashlib.sha256(p.read_bytes()).hexdigest(),
}},sort_keys=True))
PY"""
    stdout,_=run(REGION,instance,command,7200)
    line=next(x for x in stdout.splitlines() if x.startswith("MAKER_HISTORICAL_RESULT="))
    return json.loads(line.split("=",1)[1])


def main()->int:
    if len(sys.argv)!=2:
        raise SystemExit("usage: v7_maker_historical_research_ssm.py REQUEST")
    repo=Path(__file__).resolve().parents[1]
    request=load_request(Path(sys.argv[1]))
    context=remote_context(request["instance_id"])
    remote="/tmp/polymarket-maker-historical-"+hashlib.sha256(
        request["request_id"].encode()).hexdigest()[:12]
    upload(request["instance_id"],remote,source_archive(repo))
    info=execute(request["instance_id"],remote,context,request)
    payload=download(request["instance_id"],remote,info)
    extract(payload,repo/request["output_directory"])
    run(REGION,request["instance_id"],f"rm -rf {remote}",60)
    print("MAKER_HISTORICAL_LOCAL_OUTPUT="+request["output_directory"])
    return 0


if __name__=="__main__":
    raise SystemExit(main())
