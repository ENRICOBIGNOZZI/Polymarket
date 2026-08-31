#!/usr/bin/env python3
"""Immutable, read-only source evidence for V7 real-PnL reconciliation.

The tape preserves raw responses from CLOB user data, the Data API and wallet /
Polygon RPC.  It cannot send orders: HTTP request metadata is restricted to
read-only GET, JSON-RPC POST, or the user WebSocket observation channel.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse


SCHEMA_VERSION = 1
RECORD_KIND = "REAL_PNL_EVIDENCE"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GENESIS_HASH = "0" * 64
SOURCES = frozenset({"CLOB_USER_WS", "CLOB_USER_TRADES", "CLOB_USER_ORDERS", "DATA_API_ACTIVITY", "DATA_API_POSITIONS", "WALLET_RPC", "POLYGON_RPC"})
USER_WS_ENDPOINT = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
USER_WS_HOST = "ws-subscriptions-clob.polymarket.com"
CLOB_REST_HOST = "clob.polymarket.com"
USER_WS_WIRE_SCHEMA = "polymarket_v7_clob_user_ws_wire_v2"
USER_WS_MAX_BYTES = 1_000_000
SOURCE_RULES = {
    "CLOB_USER_WS": ("WS", "wss"),
    "CLOB_USER_TRADES": ("GET", "https"),
    "CLOB_USER_ORDERS": ("GET", "https"),
    "DATA_API_ACTIVITY": ("GET", "https"),
    "DATA_API_POSITIONS": ("GET", "https"),
    "WALLET_RPC": ("POST", "https"),
    "POLYGON_RPC": ("POST", "https"),
}


class EvidenceError(ValueError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _nonempty(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{name}:missing")
    return value.strip()


def _timestamp(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise EvidenceError(f"{name}:invalid")
    return value


def _json_object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise EvidenceError("clob_user_ws:duplicate_json_key")
        value[key] = item
    return value


def _ws_text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"clob_user_ws:{name}:missing")
    return value


def _ws_decimal(name: str, value: Any) -> str:
    rendered = _ws_text(name, value)
    try:
        numeric = float(rendered)
    except ValueError as exc:
        raise EvidenceError(f"clob_user_ws:{name}:invalid") from exc
    if not numeric > 0.0 or numeric == float("inf"):
        raise EvidenceError(f"clob_user_ws:{name}:invalid")
    return rendered


def _ws_nonnegative_decimal(name: str, value: Any) -> str:
    rendered = _ws_text(name, value)
    try:
        numeric = float(rendered)
    except ValueError as exc:
        raise EvidenceError(f"clob_user_ws:{name}:invalid") from exc
    if numeric < 0.0 or numeric == float("inf"):
        raise EvidenceError(f"clob_user_ws:{name}:invalid")
    return rendered


def _ws_timestamp(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise EvidenceError("clob_user_ws:timestamp:invalid")
    rendered = str(value)
    if not re.fullmatch(r"[0-9]+", rendered) or int(rendered) <= 0:
        raise EvidenceError("clob_user_ws:timestamp:invalid")
    return rendered


def parse_clob_user_ws_wire(wire_json: str) -> dict[str, Any]:
    """Strictly decode one documented V2 authenticated user-stream frame.

    The caller retains the original UTF-8 JSON string; parsing is only for
    structural validation and independent replay.  Subscription/auth frames and
    PING/PONG are intentionally not evidence because they are not economic
    observations.
    """
    if not isinstance(wire_json, str) or not wire_json:
        raise EvidenceError("clob_user_ws:wire_missing")
    try:
        encoded = wire_json.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise EvidenceError("clob_user_ws:wire_not_utf8") from exc
    if len(encoded) > USER_WS_MAX_BYTES:
        raise EvidenceError("clob_user_ws:wire_too_large")
    try:
        event = json.loads(wire_json, object_pairs_hook=_json_object_without_duplicate_keys)
    except (json.JSONDecodeError, EvidenceError) as exc:
        raise EvidenceError("clob_user_ws:wire_not_json_object") from exc
    if not isinstance(event, dict):
        raise EvidenceError("clob_user_ws:wire_not_json_object")
    if set(event) != {"topic", "type", "payload"} or event.get("topic") != "user":
        raise EvidenceError("clob_user_ws:frame_shape")
    event_type = event.get("type")
    if event_type not in {"order", "trade"}:
        raise EvidenceError("clob_user_ws:event_type")
    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise EvidenceError("clob_user_ws:payload_shape")
    for field in ("id", "owner", "market", "tokenId", "side", "timestamp"):
        _ws_text(field, payload.get(field)) if field != "timestamp" else _ws_timestamp(payload.get(field))
    if payload["side"] not in {"BUY", "SELL"}:
        raise EvidenceError("clob_user_ws:side")
    if event_type == "order":
        required = {"id", "owner", "market", "tokenId", "side", "originalSize", "sizeMatched",
                    "price", "orderEventType", "status", "timestamp"}
        if not required.issubset(payload):
            raise EvidenceError("clob_user_ws:order_shape")
        if payload["orderEventType"] not in {"PLACEMENT", "UPDATE", "CANCELLATION"}:
            raise EvidenceError("clob_user_ws:order_event_type")
        if payload["status"] not in {"LIVE", "MATCHED", "DELAYED", "UNMATCHED", "CANCELED"}:
            raise EvidenceError("clob_user_ws:order_status")
        _ws_decimal("original_size", payload["originalSize"])
        # An empty matched amount is not a valid economic update; zero is.
        try:
            matched = float(str(payload["sizeMatched"]))
        except ValueError as exc:
            raise EvidenceError("clob_user_ws:size_matched:invalid") from exc
        if matched < 0.0 or matched == float("inf"):
            raise EvidenceError("clob_user_ws:size_matched:invalid")
        _ws_decimal("price", payload["price"])
        return {"event_type": "order", "id": _ws_text("id", payload["id"]),
                "owner": _ws_text("owner", payload["owner"]), "market": _ws_text("market", payload["market"]),
                "asset_id": _ws_text("tokenId", payload["tokenId"]), "side": payload["side"],
                "original_size": _ws_text("originalSize", payload["originalSize"]),
                "size_matched": str(payload["sizeMatched"]), "price": _ws_text("price", payload["price"]),
                "order_event_type": payload["orderEventType"], "status": payload["status"],
                "timestamp": _ws_timestamp(payload["timestamp"])}
    required = {"id", "takerOrderId", "market", "tokenId", "side", "size", "price", "status", "owner", "timestamp"}
    if not required.issubset(payload):
        raise EvidenceError("clob_user_ws:trade_shape")
    if payload["status"] not in {"TRADE_STATUS_MATCHED", "TRADE_STATUS_MATCHED_NOT_BROADCASTED",
                                 "TRADE_STATUS_MINED", "TRADE_STATUS_CONFIRMED",
                                 "TRADE_STATUS_RETRYING", "TRADE_STATUS_FAILED"}:
        raise EvidenceError("clob_user_ws:trade_status")
    _ws_decimal("size", payload["size"])
    _ws_decimal("price", payload["price"])
    normalized = {"event_type": "trade", "id": _ws_text("id", payload["id"]),
            "taker_order_id": _ws_text("takerOrderId", payload["takerOrderId"]),
            "owner": _ws_text("owner", payload["owner"]), "market": _ws_text("market", payload["market"]),
            "asset_id": _ws_text("tokenId", payload["tokenId"]), "side": payload["side"],
            "size": _ws_text("size", payload["size"]), "price": _ws_text("price", payload["price"]),
            "status": payload["status"], "timestamp": _ws_timestamp(payload["timestamp"])}
    if "traderSide" in payload:
        if payload["traderSide"] not in {"TAKER", "MAKER"}:
            raise EvidenceError("clob_user_ws:trader_side")
        normalized["trader_side"] = payload["traderSide"]
    if "feeRateBps" in payload:
        normalized["fee_rate_bps"] = _ws_nonnegative_decimal("feeRateBps", payload["feeRateBps"])
    return normalized


def clob_user_ws_record(model_sha: str, received_ts_ms: int, wire_json: str) -> "EvidenceRecord":
    """Convert a captured user-stream event into one unsealed evidence record.

    `EvidenceTapeWriter.append` is still the only operation that seals the
    record into the append-only tape. The exact wire string and its byte hash
    are both retained, preventing a parser rewrite from changing the observed
    CLOB payload.
    """
    event = parse_clob_user_ws_wire(wire_json)
    try:
        wire_hash = hashlib.sha256(wire_json.encode("utf-8")).hexdigest()
    except UnicodeEncodeError as exc:  # parse_clob_user_ws_wire normally catches this.
        raise EvidenceError("clob_user_ws:wire_not_utf8") from exc
    return EvidenceRecord(
        model_sha=model_sha,
        source="CLOB_USER_WS",
        source_record_id=f"{event['event_type']}:{event['id']}:{wire_hash}",
        received_ts_ms=received_ts_ms,
        request_method="WS",
        endpoint=USER_WS_ENDPOINT,
        authenticated_read=True,
        response={"schema": USER_WS_WIRE_SCHEMA, "wire_json": wire_json,
                  "wire_sha256": wire_hash, "event": event},
    )


@dataclass(frozen=True)
class EvidenceRecord:
    model_sha: str
    source: str
    source_record_id: str
    received_ts_ms: int
    request_method: str
    endpoint: str
    response: Any
    query: dict[str, str] = field(default_factory=dict)
    authenticated_read: bool = False
    record_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    schema_version: int = SCHEMA_VERSION
    previous_record_hash: str = GENESIS_HASH
    record_hash: str | None = None

    def _hash_payload(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("record_hash", None)
        value["record_kind"] = RECORD_KIND
        return value

    def computed_hash(self) -> str:
        return sha256(self._hash_payload())

    def validate(self, *, sealed: bool = True) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise EvidenceError("schema_version:unsupported")
        if not isinstance(self.model_sha, str) or not SHA_RE.fullmatch(self.model_sha):
            raise EvidenceError("model_sha:not_exact_git_sha")
        if self.source not in SOURCES:
            raise EvidenceError("source:unsupported")
        _nonempty("source_record_id", self.source_record_id)
        _nonempty("record_id", self.record_id)
        _timestamp("received_ts_ms", self.received_ts_ms)
        method, scheme = SOURCE_RULES[self.source]
        if self.request_method != method:
            raise EvidenceError("request_method:not_read_only_source_method")
        parsed = urlparse(self.endpoint)
        if parsed.scheme != scheme or not parsed.netloc:
            raise EvidenceError("endpoint:invalid")
        if self.source == "CLOB_USER_WS" and (parsed.netloc != USER_WS_HOST or parsed.path != "/ws/user"):
            raise EvidenceError("endpoint:not_polymarket")
        if self.source in {"CLOB_USER_TRADES", "CLOB_USER_ORDERS"} and parsed.netloc != CLOB_REST_HOST:
            raise EvidenceError("endpoint:not_polymarket")
        if self.source.startswith("DATA_API_") and parsed.netloc != "data-api.polymarket.com":
            raise EvidenceError("endpoint:not_data_api")
        if not isinstance(self.authenticated_read, bool):
            raise EvidenceError("authenticated_read:not_boolean")
        if self.source.startswith("CLOB_USER_") and not self.authenticated_read:
            raise EvidenceError("authenticated_read:required")
        if not isinstance(self.query, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in self.query.items()):
            raise EvidenceError("query:invalid")
        encoded = canonical_bytes(self.response)
        if not encoded:
            raise EvidenceError("response:invalid")
        if self.source == "CLOB_USER_WS":
            if not isinstance(self.response, dict) or set(self.response) != {
                    "schema", "wire_json", "wire_sha256", "event"}:
                raise EvidenceError("clob_user_ws:response_shape")
            if self.response["schema"] != USER_WS_WIRE_SCHEMA:
                raise EvidenceError("clob_user_ws:response_schema")
            wire_json = self.response["wire_json"]
            event = parse_clob_user_ws_wire(wire_json)
            try:
                wire_hash = hashlib.sha256(wire_json.encode("utf-8")).hexdigest()
            except UnicodeEncodeError as exc:
                raise EvidenceError("clob_user_ws:wire_not_utf8") from exc
            if self.response["wire_sha256"] != wire_hash or self.response["event"] != event:
                raise EvidenceError("clob_user_ws:wire_or_event_mismatch")
            if self.source_record_id != f"{event['event_type']}:{event['id']}:{wire_hash}":
                raise EvidenceError("clob_user_ws:source_record_id_mismatch")
        if not isinstance(self.previous_record_hash, str) or not SHA256_RE.fullmatch(self.previous_record_hash):
            raise EvidenceError("previous_record_hash:invalid")
        if sealed:
            if not isinstance(self.record_hash, str) or not SHA256_RE.fullmatch(self.record_hash):
                raise EvidenceError("record_hash:invalid")
            if self.record_hash != self.computed_hash():
                raise EvidenceError("record_hash:mismatch")
        elif self.record_hash is not None:
            raise EvidenceError("unsealed_record_has_hash")

    def seal(self, previous_hash: str) -> "EvidenceRecord":
        if not isinstance(previous_hash, str) or not SHA256_RE.fullmatch(previous_hash):
            raise EvidenceError("previous_record_hash:invalid")
        candidate = replace(self, previous_record_hash=previous_hash, record_hash=None)
        candidate.validate(sealed=False)
        sealed = replace(candidate, record_hash=candidate.computed_hash())
        sealed.validate()
        return sealed

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {"record_kind": RECORD_KIND, **asdict(self)}

    @classmethod
    def from_dict(cls, raw: Any) -> "EvidenceRecord":
        if not isinstance(raw, dict) or raw.get("record_kind") != RECORD_KIND:
            raise EvidenceError("record_kind:invalid")
        value = dict(raw)
        value.pop("record_kind", None)
        try:
            record = cls(**value)
        except TypeError as exc:
            raise EvidenceError(f"record:shape:{exc}") from exc
        record.validate()
        return record


def evidence_path(run_root: Path) -> Path:
    return Path(run_root) / "evidence" / "real_pnl.jsonl"


def iter_records(path: Path) -> Iterator[EvidenceRecord]:
    tips: dict[str, str] = {}
    source_ids: dict[str, set[tuple[str, str]]] = {}
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = EvidenceRecord.from_dict(json.loads(line))
            except (json.JSONDecodeError, EvidenceError) as exc:
                raise EvidenceError(f"line_{line_number}:{exc}") from exc
            expected = tips.get(record.model_sha, GENESIS_HASH)
            if record.previous_record_hash != expected:
                raise EvidenceError(f"line_{line_number}:chain_break")
            identities = source_ids.setdefault(record.model_sha, set())
            identity = (record.source, record.source_record_id)
            if identity in identities:
                raise EvidenceError(f"line_{line_number}:duplicate_source_record")
            identities.add(identity)
            tips[record.model_sha] = str(record.record_hash)
            yield record


class EvidenceTapeWriter:
    """Exclusive append writer; raw source evidence cannot be overwritten."""

    def __init__(self, path: Path, *, writer_id: str, model_sha: str):
        self.path = Path(path)
        self.writer_id = _nonempty("writer_id", writer_id)
        if not SHA_RE.fullmatch(model_sha):
            raise EvidenceError("model_sha:not_exact_git_sha")
        self.model_sha = model_sha
        self.owner_path = self.path.with_suffix(self.path.suffix + ".writer.json")
        self.token = uuid.uuid4().hex
        self._owned = False
        self._tip: str | None = None

    def acquire(self) -> None:
        if self._owned:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.owner_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise EvidenceError("evidence_already_owned") from exc
        try:
            os.write(fd, canonical_bytes({"writer_id": self.writer_id, "model_sha": self.model_sha, "token": self.token}) + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        self._owned = True

    def _last_hash(self) -> str:
        if self._tip is not None:
            return self._tip
        tip = GENESIS_HASH
        if self.path.exists():
            for record in iter_records(self.path):
                if record.model_sha == self.model_sha:
                    tip = str(record.record_hash)
        self._tip = tip
        return tip

    def append(self, record: EvidenceRecord) -> EvidenceRecord:
        if not self._owned:
            raise EvidenceError("writer:not_acquired")
        if record.model_sha != self.model_sha:
            raise EvidenceError("writer:mixed_model_sha")
        sealed = record.seal(self._last_hash())
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(sealed.to_dict(), sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._tip = str(sealed.record_hash)
        return sealed

    def close(self) -> None:
        if not self._owned:
            return
        try:
            owner = json.loads(self.owner_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EvidenceError("owner:unreadable") from exc
        if owner.get("token") != self.token:
            raise EvidenceError("owner:mismatch")
        self.owner_path.unlink()
        self._owned = False

    def __enter__(self) -> "EvidenceTapeWriter":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def manifest(path: Path, *, model_sha: str) -> dict[str, Any]:
    if not SHA_RE.fullmatch(model_sha):
        raise EvidenceError("model_sha:not_exact_git_sha")
    records = [record for record in iter_records(path) if record.model_sha == model_sha]
    return {
        "schema": "polymarket_v7_real_pnl_evidence_manifest_v1",
        "model_sha": model_sha,
        "records": len(records),
        "head_hash": records[-1].record_hash if records else None,
        "record_hashes": [record.record_hash for record in records],
        "sources": sorted({record.source for record in records}),
    }


def main() -> int:
    raise SystemExit("library_only: collectors must call EvidenceTapeWriter with captured read-only responses")


if __name__ == "__main__":
    main()
