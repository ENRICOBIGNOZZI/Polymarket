"""Public CLOB terms for PAPER simulation; no authentication or order API."""
from __future__ import annotations
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

SCHEMA = "polymarket_v7_market_execution_terms_v1"
CONDITION = re.compile(r"0x[0-9a-fA-F]{64}\Z")
CLOB = "https://clob.polymarket.com/clob-markets/"


def snapshot(market: dict[str, Any], fetch: Callable[[str], Any],
             *, now_ns: int | None = None) -> dict[str, Any]:
    condition = str(market.get("condition_id") or "")
    observed = time.time_ns() if now_ns is None else now_ns
    result: dict[str, Any] = {
        "schema": SCHEMA, "paper_only": True, "execution_authority": False,
        "market_id": str(market.get("market_id") or ""),
        "condition_id": condition, "observed_at_ns": observed,
        "source": CLOB + condition if CONDITION.fullmatch(condition) else None,
        "state": "UNKNOWN", "itode": None, "mandatory_taker_delay_ns": None,
        "delay_rule_source": "POLYMARKET_ORDER_LIFECYCLE_20260919",
        "raw_response": None, "reason": "condition_id_invalid",
    }
    if CONDITION.fullmatch(condition):
        try:
            raw = fetch(CLOB + condition)
            json.dumps(raw, allow_nan=False)
            result["raw_response"] = raw
            if not isinstance(raw, dict) or type(raw.get("itode")) is not bool:
                raise ValueError("itode_missing_or_not_boolean")
            rows = raw.get("t")
            tokens = [str(x.get("t") or "") for x in rows if isinstance(x, dict)] if isinstance(rows, list) else []
            expected = [str(x) for x in market.get("clob_token_ids", [])]
            if len(tokens) != 2 or len(set(tokens)) != 2 or not all(tokens) or set(tokens) != set(expected):
                raise ValueError("market_token_binding_mismatch")
            explicit_condition = raw.get("condition_id", condition)
            if explicit_condition != condition:
                raise ValueError("condition_id_mismatch")
            result.update(state="VERIFIED_SNAPSHOT", reason=None, itode=raw["itode"],
                          mandatory_taker_delay_ns=250_000_000 if raw["itode"] else 0)
        except (OSError, ValueError, RuntimeError, TypeError) as exc:
            result["reason"] = type(exc).__name__ + ":" + str(exc)[:200]
    body = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
    result["snapshot_sha256"] = hashlib.sha256(body.encode()).hexdigest()
    return result


def persist(root: Path, value: dict[str, Any]) -> Path:
    """Immutable evidence; existing content must match, never overwrite."""
    digest = value["snapshot_sha256"]
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("invalid_snapshot_hash")
    directory = root / "control" / "market_execution_terms" / "by-hash"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (digest + ".json")
    data = json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    verified = dict(value)
    verified.pop("snapshot_sha256")
    canonical = json.dumps(verified, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if hashlib.sha256(canonical.encode()).hexdigest() != digest:
        raise ValueError("snapshot_content_hash_mismatch")
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            import os
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.is_symlink() or path.read_text(encoding="utf-8") != data:
            raise ValueError("immutable_market_terms_conflict")
    return path
