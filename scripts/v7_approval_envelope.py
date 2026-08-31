#!/usr/bin/env python3
"""Fail-closed validation for private V7 live-approval envelopes."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Callable

SHA40=re.compile(r"^[0-9a-f]{40}$"); SHA256=re.compile(r"^[0-9a-f]{64}$")
LIVE_MODES={"MICRO_LIVE","LIVE_RESTRICTED","LIVE_SCALED"}; ORDER_TYPES={"GTC","GTD","FOK","FAK"}
FIELDS={"schema_version","exact_code_sha","build_manifest_hash","config_bundle_hash","policy_hash","model_hashes","wallet","session_key_id","allowed_execution_mode","allowed_condition_ids","allowed_token_ids","allowed_order_types","require_post_only","maximum_order_base_units","maximum_gross_exposure_base_units","maximum_event_loss_base_units","maximum_daily_loss_base_units","maximum_open_order_count","start_timestamp","expiry_timestamp","approver_identity","approval_nonce","signature"}
class ApprovalEnvelopeError(ValueError): pass
def canonical_payload(value: dict[str,Any])->bytes:
    value=dict(value); value.pop("signature",None)
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode()
def envelope_hash(value: dict[str,Any])->str: return hashlib.sha256(canonical_payload(value)).hexdigest()
def _time(name:str,value:Any)->datetime:
    try: result=datetime.fromisoformat(value.replace("Z","+00:00")) if isinstance(value,str) else None
    except ValueError as exc: raise ApprovalEnvelopeError(f"{name}:invalid") from exc
    if result is None or result.tzinfo is None: raise ApprovalEnvelopeError(f"{name}:timezone_required")
    return result.astimezone(timezone.utc)
def _list(name:str,value:Any,allowed:set[str]|None=None)->None:
    if not isinstance(value,list) or not value or not all(isinstance(x,str) and x for x in value): raise ApprovalEnvelopeError(f"{name}:nonempty_strings_required")
    if len(value)!=len(set(value)): raise ApprovalEnvelopeError(f"{name}:duplicates")
    if allowed is not None and not set(value)<=allowed: raise ApprovalEnvelopeError(f"{name}:unsupported")
def validate_structure(value:Any,*,now:datetime|None=None)->dict[str,Any]:
    if not isinstance(value,dict) or set(value)!=FIELDS: raise ApprovalEnvelopeError("envelope:shape")
    if value["schema_version"]!=1 or not SHA40.fullmatch(str(value["exact_code_sha"])): raise ApprovalEnvelopeError("envelope:identity")
    for key in ("build_manifest_hash","config_bundle_hash","policy_hash"):
        if not SHA256.fullmatch(str(value[key])): raise ApprovalEnvelopeError(f"{key}:invalid")
    models=value["model_hashes"]
    if not isinstance(models,dict) or not models or not all(isinstance(k,str) and k and SHA256.fullmatch(str(v)) for k,v in models.items()): raise ApprovalEnvelopeError("model_hashes:invalid")
    for key in ("wallet","session_key_id","approver_identity","approval_nonce","signature"):
        if not isinstance(value[key],str) or not value[key].strip(): raise ApprovalEnvelopeError(f"{key}:missing")
    if len(value["approval_nonce"])<16 or value["allowed_execution_mode"] not in LIVE_MODES: raise ApprovalEnvelopeError("envelope:approval_scope")
    _list("allowed_condition_ids",value["allowed_condition_ids"]);_list("allowed_token_ids",value["allowed_token_ids"]);_list("allowed_order_types",value["allowed_order_types"],ORDER_TYPES)
    if value["require_post_only"] is not True: raise ApprovalEnvelopeError("require_post_only:required")
    for key in ("maximum_order_base_units","maximum_gross_exposure_base_units","maximum_event_loss_base_units","maximum_daily_loss_base_units","maximum_open_order_count"):
        if isinstance(value[key],bool) or not isinstance(value[key],int) or value[key]<=0: raise ApprovalEnvelopeError(f"{key}:positive_integer_required")
    start,expiry=_time("start_timestamp",value["start_timestamp"]),_time("expiry_timestamp",value["expiry_timestamp"]); instant=(now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if start>=expiry or instant<start or instant>expiry: raise ApprovalEnvelopeError("approval_window:not_active")
    return value
def verify_signature(value:dict[str,Any],verifier:Callable[[bytes,str,str],bool]|None)->None:
    if verifier is None: raise ApprovalEnvelopeError("signature_verifier_required")
    try: valid=verifier(canonical_payload(value),value["signature"],value["approver_identity"])
    except Exception as exc: raise ApprovalEnvelopeError("signature_verifier_failed") from exc
    if valid is not True: raise ApprovalEnvelopeError("signature_invalid")
def authorize_intent(envelope:dict[str,Any],intent:dict[str,Any])->None:
    for a,b in (("exact_code_sha","exact_code_sha"),("build_manifest_hash","build_manifest_hash"),("config_bundle_hash","config_bundle_hash"),("policy_hash","policy_hash"),("execution_mode","allowed_execution_mode")):
        if intent.get(a)!=envelope[b]: raise ApprovalEnvelopeError(f"intent:{a}:mismatch")
    if intent.get("condition_id") not in envelope["allowed_condition_ids"] or intent.get("token_id") not in envelope["allowed_token_ids"]: raise ApprovalEnvelopeError("intent:market_not_allowlisted")
    if intent.get("order_type") not in envelope["allowed_order_types"] or intent.get("post_only") is not True: raise ApprovalEnvelopeError("intent:order_type_not_allowed")
    if isinstance(intent.get("size_base_units"),bool) or not isinstance(intent.get("size_base_units"),int) or not 0<intent["size_base_units"]<=envelope["maximum_order_base_units"]: raise ApprovalEnvelopeError("intent:size_limit")
    for key,limit in (("gross_exposure_base_units","maximum_gross_exposure_base_units"),("event_loss_base_units","maximum_event_loss_base_units"),("daily_loss_base_units","maximum_daily_loss_base_units"),("open_order_count","maximum_open_order_count")):
        candidate=intent.get(key)
        if isinstance(candidate,bool) or not isinstance(candidate,int) or candidate<0 or candidate>envelope[limit]: raise ApprovalEnvelopeError(f"intent:{key}:limit")
