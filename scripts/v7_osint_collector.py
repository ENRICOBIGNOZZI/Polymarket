#!/usr/bin/env python3
"""Collect authoritative OSINT sources into a causal receive-time research tape.

The collector has no execution, OMS, inventory, capital, or promotion authority.
It uses conditional HTTP, preserves corrections as new events, and never rewrites
the append-only tape. Source reliability fields remain unvalidated priors until
forward observations replace them with empirical estimates.
"""
from __future__ import annotations

import argparse
import email.utils
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from v7_osint_engine import CollectorSource, CollectorSourceCatalog, RawEvent
from v7_osint_pipeline import (
    AuthoritativeSource,
    CausalEventTape,
    SourceDocument,
    SourceRegistry,
    ingest_document,
)

TAPE_SCHEMA = "polymarket_v7_osint_raw_tape_v1"
STATE_SCHEMA = "polymarket_v7_osint_collector_state_v1"
STATUS_SCHEMA = "polymarket_v7_osint_collector_status_v1"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class CollectorError(ValueError):
    pass


def _origin(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise CollectorError("source_endpoint_must_be_verified_https")
    port = f":{parsed.port}" if parsed.port and parsed.port != 443 else ""
    return f"https://{parsed.hostname.lower()}{port}"


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _text(element: ET.Element, name: str) -> str:
    for child in element.iter():
        if _local_name(child.tag) == name and child.text:
            return " ".join(child.text.split())
    return ""


def _link(element: ET.Element) -> str:
    for child in element.iter():
        if _local_name(child.tag) == "link":
            return str(child.attrib.get("href") or child.text or "").strip()
    return ""


def timestamp_ms(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        parsed = email.utils.parsedate_to_datetime(text)
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp() * 1000)
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except (TypeError, ValueError, OverflowError):
        return 0


def fetch(source: CollectorSource, conditional: Mapping[str, Any], timeout: float = 20.0) -> HttpResponse:
    headers = {
        "Accept": "application/atom+xml, application/rss+xml, application/json;q=0.9, */*;q=0.1",
        "User-Agent": os.environ.get(
            "PM_V7_OSINT_USER_AGENT",
            "PolymarketV7Research/1.0 (https://github.com/ENRICOBIGNOZZI/Polymarket)",
        ),
    }
    if conditional.get("etag"):
        headers["If-None-Match"] = str(conditional["etag"])
    if conditional.get("last_modified"):
        headers["If-Modified-Since"] = str(conditional["last_modified"])
    request = urllib.request.Request(source.endpoint, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise CollectorError("source_response_too_large")
            return HttpResponse(int(response.status), dict(response.headers.items()), body)
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return HttpResponse(304, dict(exc.headers.items()), b"")
        raise


def _canonical_item(source: CollectorSource, item: Mapping[str, Any], received_ts_ms: int) -> dict[str, Any] | None:
    source_event_id = str(item.get("source_event_id") or "").strip()
    published_ts_ms = int(item.get("published_ts_ms") or 0)
    title = " ".join(str(item.get("title") or "").split())
    summary = " ".join(str(item.get("summary") or "").split())
    url = str(item.get("url") or "").strip()
    if (not source_event_id or published_ts_ms <= 0 or published_ts_ms > received_ts_ms or
            received_ts_ms - published_ts_ms > source.max_age_ms):
        return None
    content = json.dumps({"title": title, "summary": summary, "url": url}, sort_keys=True,
                         separators=(",", ":"), ensure_ascii=False)
    payload_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {
        "source_event_id": source_event_id,
        "published_ts_ms": published_ts_ms,
        "payload_hash": payload_hash,
        "content": content,
    }


def parse_federal_register(body: bytes) -> tuple[dict[str, Any], ...]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectorError("invalid_federal_register_json") from exc
    rows = value.get("results") if isinstance(value, dict) else None
    if not isinstance(rows, list):
        raise CollectorError("invalid_federal_register_results")
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        agencies = row.get("agencies") if isinstance(row.get("agencies"), list) else []
        agency_names = [str(x.get("name") or "") for x in agencies if isinstance(x, dict)]
        out.append({
            "source_event_id": row.get("document_number"),
            "published_ts_ms": timestamp_ms(row.get("publication_date")),
            "title": row.get("title"),
            "summary": " | ".join([str(row.get("abstract") or ""), ", ".join(agency_names)]).strip(" |"),
            "url": row.get("html_url") or row.get("json_url"),
        })
    return tuple(out)


def parse_xml_feed(body: bytes, *, atom: bool) -> tuple[dict[str, Any], ...]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise CollectorError("invalid_xml_feed") from exc
    target = "entry" if atom else "item"
    out = []
    for item in root.iter():
        if _local_name(item.tag) != target:
            continue
        url = _link(item)
        source_event_id = _text(item, "id") or _text(item, "guid") or url
        out.append({
            "source_event_id": source_event_id,
            "published_ts_ms": timestamp_ms(
                _text(item, "published") or _text(item, "updated") or _text(item, "pubdate")
            ),
            "title": _text(item, "title"),
            "summary": _text(item, "summary") or _text(item, "description"),
            "url": url,
        })
    return tuple(out)


def parse_source(source: CollectorSource, body: bytes) -> tuple[dict[str, Any], ...]:
    if source.parser == "FEDERAL_REGISTER_JSON":
        return parse_federal_register(body)
    if source.parser == "ATOM":
        return parse_xml_feed(body, atom=True)
    if source.parser == "RSS":
        return parse_xml_feed(body, atom=False)
    raise CollectorError("unsupported_source_parser")


def materialize_events(
    source: CollectorSource, items: Sequence[Mapping[str, Any]], *, received_ts_ms: int,
    source_state: Mapping[str, Any], connection_epoch: int,
    authority_registry: SourceRegistry | None = None,
) -> tuple[RawEvent, ...]:
    if len(source.event_types) != 1:
        raise CollectorError("collector_requires_one_deterministic_event_family")
    previous = source_state.get("seen") if isinstance(source_state.get("seen"), dict) else {}
    events: list[RawEvent] = []
    for item in items:
        canonical = _canonical_item(source, item, received_ts_ms)
        if canonical is None:
            continue
        source_event_id = canonical["source_event_id"]
        old = previous.get(source_event_id) if isinstance(previous.get(source_event_id), dict) else {}
        if old.get("payload_hash") == canonical["payload_hash"]:
            continue
        root = hashlib.sha256(f"{source.source_id}:{source_event_id}".encode()).hexdigest()
        registry = authority_registry or SourceRegistry((AuthoritativeSource(
            source.source_id, source.entity, source.authority_tier,
            (_origin(source.endpoint),), source.event_types, source.adapter_version,
        ),))
        event = ingest_document(registry, SourceDocument(
            source_id=source.source_id, source_url=source.endpoint,
            source_event_id=source_event_id, root_lineage_id=root,
            event_family=source.event_types[0], entity=source.entity,
            published_ts_ms=canonical["published_ts_ms"], received_ts_ms=received_ts_ms,
            payload=canonical["content"].encode("utf-8"),
            advertised_payload_sha256=canonical["payload_hash"],
            correction_of=str(old.get("event_id") or ""), extracted_by_llm=False,
        ))
        events.append(replace(event, content=canonical["content"],
                              transport=source.transport.value,
                              connection_epoch=connection_epoch))
    return tuple(sorted(events, key=lambda x: (x.published_ts_ms, x.source_event_id, x.event_id)))


def load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema": STATE_SCHEMA, "sources": {}}
    if value.get("schema") != STATE_SCHEMA or not isinstance(value.get("sources"), dict):
        raise CollectorError("collector_state_schema_mismatch")
    return value


def recover_recent_tape(path: Path, limit: int = 20_000) -> dict[str, dict[str, dict[str, Any]]]:
    """Recover dedup/correction lineage if tape append won a crash race with state."""
    records = CausalEventTape(path).read_verified()
    recovered: dict[str, dict[str, dict[str, Any]]] = {}
    for record in records[-max(1, limit):]:
        if record.kind != "RAW_EVENT":
            continue
        row = record.payload
        source_id = str(row.get("source_id") or "")
        source_event_id = str(row.get("source_event_id") or "")
        event_id = str(row.get("event_id") or "")
        payload_hash = str(row.get("payload_hash") or "")
        if not all((source_id, source_event_id, event_id, payload_hash)):
            continue
        recovered.setdefault(source_id, {})[source_event_id] = {
            "event_id": event_id, "payload_hash": payload_hash,
            "received_ts_ms": int(row.get("received_ts_ms") or 0),
        }
    return recovered


def atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def append_tape(path: Path, events: Sequence[RawEvent], *, source_registry_sha: str) -> None:
    tape = CausalEventTape(path)
    for event in events:
        tape.append_event(event, source_registry_sha=source_registry_sha)


def collect_once(
    registry: CollectorSourceCatalog, *, tape_path: Path, state_path: Path,
    fetcher: Callable[[CollectorSource, Mapping[str, Any], float], HttpResponse] = fetch,
    now_ms: int | None = None, timeout: float = 20.0,
) -> dict[str, Any]:
    started_ms = time.time_ns() // 1_000_000 if now_ms is None else int(now_ms)
    state = load_state(state_path)
    source_states = state.setdefault("sources", {})
    needs_recovery = tape_path.exists() and (
        not state_path.exists() or tape_path.stat().st_mtime_ns > state_path.stat().st_mtime_ns
    )
    recovered = recover_recent_tape(tape_path) if needs_recovery else {}
    authority_registry = SourceRegistry(tuple(AuthoritativeSource(
        source.source_id, source.entity, source.authority_tier,
        (_origin(source.endpoint),), source.event_types, source.adapter_version,
    ) for source in registry.enabled()))
    statuses: list[dict[str, Any]] = []
    total_events = 0
    for source in registry.enabled():
        current = source_states.get(source.source_id)
        if not isinstance(current, dict):
            current = {"connection_epoch": 0, "seen": {}}
        if recovered.get(source.source_id):
            current["seen"] = {**recovered[source.source_id], **dict(current.get("seen") or {})}
        epoch = int(current.get("connection_epoch") or 0) + 1
        try:
            response = fetcher(source, current, timeout)
            received_ms = started_ms if now_ms is not None else time.time_ns() // 1_000_000
            if response.status == 304:
                events: tuple[RawEvent, ...] = ()
            elif response.status == 200:
                items = parse_source(source, response.body)
                events = materialize_events(source, items, received_ts_ms=received_ms,
                                            source_state=current, connection_epoch=epoch,
                                            authority_registry=authority_registry)
                append_tape(tape_path, events,
                            source_registry_sha=authority_registry.registry_sha)
                seen = dict(current.get("seen") or {})
                for event in events:
                    seen[event.source_event_id] = {
                        "event_id": event.event_id, "payload_hash": event.payload_hash,
                        "received_ts_ms": event.received_ts_ms,
                    }
                if len(seen) > 10_000:
                    newest = sorted(seen.items(), key=lambda pair: int(pair[1].get("received_ts_ms") or 0),
                                    reverse=True)[:10_000]
                    seen = dict(newest)
                current["seen"] = seen
            else:
                raise CollectorError(f"unexpected_http_status:{response.status}")
            current.update({
                "connection_epoch": epoch,
                "etag": response.headers.get("ETag") or response.headers.get("etag") or current.get("etag", ""),
                "last_modified": response.headers.get("Last-Modified") or
                                 response.headers.get("last-modified") or current.get("last_modified", ""),
                "last_success_receive_ts_ms": received_ms,
                "last_error": "",
            })
            total_events += len(events)
            statuses.append({"source_id": source.source_id, "healthy": True,
                             "http_status": response.status, "new_events": len(events), "blocker": ""})
        except Exception as exc:
            current.update({"connection_epoch": epoch, "last_error": f"{type(exc).__name__}:{exc}"})
            statuses.append({"source_id": source.source_id, "healthy": False, "http_status": 0,
                             "new_events": 0, "blocker": current["last_error"]})
        source_states[source.source_id] = current
    state["updated_ts_ms"] = started_ms
    atomic_json(state_path, state)
    return {
        "schema": STATUS_SCHEMA, "timestamp_ms": started_ms, "paper_only": True,
        "authenticated_execution": False, "real_order_submission": False,
        "authority": "RESEARCH", "automatic_promotion": False,
        "source_registry_sha": authority_registry.registry_sha,
        "enabled_sources": len(registry.enabled()), "new_events": total_events,
        "healthy_sources": sum(bool(x["healthy"]) for x in statuses), "sources": statuses,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=Path("config/v7_osint_sources.json"))
    parser.add_argument("--tape", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--status", type=Path)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args(argv)
    registry = CollectorSourceCatalog.load(args.registry)
    while True:
        result = collect_once(registry, tape_path=args.tape, state_path=args.state,
                              timeout=max(0.1, args.timeout))
        if args.status:
            atomic_json(args.status, result)
        print(json.dumps(result, sort_keys=True), flush=True)
        if not args.loop:
            return 0
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
