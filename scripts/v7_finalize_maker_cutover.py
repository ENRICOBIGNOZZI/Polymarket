#!/usr/bin/env python3
"""Prove the canonical PAPER Maker is flat after drain and runtime stop."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from v7_maker_accounting import authorized_maker_flat_proof

SHA40 = re.compile(r"^[0-9a-f]{40}$")

class MakerCutoverError(RuntimeError):
    pass

def read_json(path: Path) -> dict[str, Any]:
    try:
        value=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError) as exc:
        raise MakerCutoverError(f"invalid_json:{path.name}") from exc
    if not isinstance(value,dict):
        raise MakerCutoverError(f"invalid_object:{path.name}")
    return value

def atomic_json(path: Path, value: dict[str,Any]) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    os.replace(tmp,path)

def digest(value: dict[str,Any]) -> str:
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":")).encode()).hexdigest()

def finalize(root: Path, model_sha: str, nonce: str, *, now_ms: int | None=None) -> dict[str,Any]:
    root=Path(root)
    if not SHA40.fullmatch(model_sha) or not nonce:
        raise MakerCutoverError("cutover_identity_invalid")
    sentinel=read_json(root/"control/CUTOVER_DRAIN")
    if (sentinel.get("schema")!="polymarket_v7_cutover_drain_v1"
            or sentinel.get("nonce")!=nonce
            or sentinel.get("current_sha")!=model_sha
            or sentinel.get("paper_only") is not True):
        raise MakerCutoverError("cutover_drain_identity_mismatch")

    account=read_json(root/"external_fair/paper_router_status.json")
    if (account.get("paper_only") is not True
            or account.get("authenticated_execution") is not False
            or account.get("real_order_submission") is not False
            or account.get("model_sha") not in (None,"",model_sha)):
        raise MakerCutoverError("paper_account_identity_invalid")
    try:
        open_positions=int(account.get("open_positions") or 0)
        pending_orders=int(account.get("pending_maker_orders") or 0)
    except (TypeError,ValueError,OverflowError) as exc:
        raise MakerCutoverError("paper_account_inventory_invalid") from exc
    if open_positions or pending_orders:
        raise MakerCutoverError("paper_account_not_flat")

    try:
        proof=authorized_maker_flat_proof(root,model_sha)
    except (ValueError,OSError) as exc:
        raise MakerCutoverError(str(exc)) from exc
    now=int(time.time_ns()//1_000_000 if now_ms is None else now_ms)
    receipt={
        "schema":"polymarket_v7_maker_cutover_flat_proof_v1",
        "state":"MAKER_FLAT",
        "timestamp_ms":now,
        "paper_only":True,
        "authenticated_execution":False,
        "real_order_submission":False,
        "model_sha":model_sha,
        "nonce":nonce,
        "paper_account_open_positions":0,
        "pending_maker_orders":0,
        "canonical_flat_proof":proof,
        "proof_sha256":digest(proof),
    }
    atomic_json(root/"control/maker_cutover_flat_proof.json",receipt)
    return receipt

def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root",type=Path,required=True)
    parser.add_argument("--model-sha",required=True)
    parser.add_argument("--nonce",required=True)
    args=parser.parse_args()
    print(json.dumps(finalize(args.run_root,args.model_sha,args.nonce),sort_keys=True))
    return 0
if __name__=="__main__":
    raise SystemExit(main())
