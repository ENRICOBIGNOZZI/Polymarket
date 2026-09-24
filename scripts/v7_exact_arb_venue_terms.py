"""Read-only, bounded public venue metadata evidence for exact-arb research.

No orders, credentials, signing or live-eligibility grant. Separate GETs are NOT
atomic. A snapshot has no invented validity lease and cannot attest terms at a
later hypothetical match. Omitted compact `itode` means false only under the
explicit official OpenAPI omission rule, after validating the descriptor binding.
Sports activation and overlapping delays remain unknown, not guessed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.request import Request, build_opener, HTTPRedirectHandler

from v7_unified_exact_arb_graph import SAFETY, GraphError, frac, fstr, sha

SCHEMA = "polymarket_v7_exact_arb_public_venue_terms_v1"
RULE = "POLYMARKET_PUBLIC_DELAY_RULE_20260924_V1"
SOURCES = {
    "openapi": "https://docs.polymarket.com/api-spec/clob-openapi.yaml",
    "lifecycle": "https://docs.polymarket.com/concepts/order-lifecycle",
    "orders": "https://docs.polymarket.com/trading/place-orders",
    "fees": "https://docs.polymarket.com/trading/fees",
}
CLOB = "https://clob.polymarket.com"
CONDITION = re.compile(r"0x[0-9a-f]{64}\Z")
TOKEN = re.compile(r"(?:0|[1-9][0-9]{0,77})\Z")
MAX_RESPONSE = 1024 * 1024


def require(condition, reason):
    if not condition:
        raise GraphError(reason)


def identity(condition, tokens):
    require(isinstance(condition, str) and CONDITION.fullmatch(condition), "venue_condition_id")
    require(isinstance(tokens, list) and 2 <= len(tokens) <= 16 and
            all(isinstance(t, str) and TOKEN.fullmatch(t) and int(t) < 2**256 for t in tokens), "venue_token_ids")
    require(len(tokens) == len(set(tokens)), "venue_duplicate_token")


def pairs(value):
    result = {}
    for key, item in value:
        require(key not in result, "venue_duplicate_json_key")
        result[key] = item
    return result


def decode(raw):
    require(isinstance(raw, bytes) and len(raw) <= MAX_RESPONSE, "venue_response_budget")
    def invalid_constant(_):
        raise GraphError("venue_nonfinite_json")
    return json.loads(raw, parse_float=str, parse_constant=invalid_constant, object_pairs_hook=pairs)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise GraphError("venue_redirect_refused")


def public_get(url):
    require(re.fullmatch(r"https://clob\.polymarket\.com/(?:clob-markets/0x[0-9a-f]{64}|markets/0x[0-9a-f]{64}|book\?token_id=[0-9]{1,78})", url),
            "venue_public_endpoint")
    with build_opener(NoRedirect()).open(Request(url, headers={"Accept": "application/json",
            "User-Agent": "Polymarket-ExactArb-Paper-Research/1.0"}), timeout=5) as response:
        require(response.status == 200, "venue_http_status")
        raw = response.read(MAX_RESPONSE + 1)
    decode(raw)  # reject invalid/truncated/duplicate-key JSON before admission
    return raw


def token_binding(rows, key, expected):
    require(isinstance(rows, list) and len(rows) == len(expected), "venue_membership_shape")
    require(all(isinstance(row, dict) and isinstance(row.get(key), str) for row in rows), "venue_membership_shape")
    tokens = [row[key] for row in rows]
    require(len(set(tokens)) == len(tokens) and set(tokens) == set(expected), "venue_token_binding")


def project(condition, tokens, compact, market, books):
    """Cross-source consistency, not proof of atomic/current execution terms."""
    identity(condition, tokens)
    require(isinstance(compact, dict) and isinstance(market, dict) and isinstance(books, dict), "venue_descriptor_shape")
    require(market.get("condition_id") == condition, "venue_condition_binding")
    for name in ("c", "condition_id"):
        if name in compact:
            require(compact[name] == condition, "venue_condition_binding")
    token_binding(compact.get("t"), "t", tokens)
    token_binding(market.get("tokens"), "token_id", tokens)
    for key, expected in (("active", True), ("closed", False), ("accepting_orders", True), ("enable_order_book", True)):
        require(market.get(key) is expected, "venue_market_not_open")
    if "archived" in market:
        require(market["archived"] is False, "venue_market_not_open")
    if "ao" in compact:
        require(compact["ao"] is True, "venue_compact_not_accepting")
    require(set(books) == set(tokens), "venue_book_membership")
    tick = frac(compact.get("mts"))
    minimum = frac(compact.get("mos"))
    require(0 < tick < 1 and minimum > 0, "venue_order_constraints")
    require(tick == frac(market.get("minimum_tick_size")) and
            minimum == frac(market.get("minimum_order_size")), "venue_order_constraints_conflict")
    for token, book in books.items():
        require(isinstance(book, dict) and book.get("asset_id") == token and book.get("market") == condition,
                "venue_book_binding")
        require(tick == frac(book.get("tick_size")) and minimum == frac(book.get("min_order_size")),
                "venue_book_order_constraints_conflict")
    fee = compact.get("fd")
    require(isinstance(fee, dict) and fee.get("to") is True, "venue_fee_unknown")
    rate, exponent = frac(fee.get("r")), frac(fee.get("e"))
    require(0 <= rate <= 1 and exponent.denominator == 1 and 0 <= exponent <= 16, "venue_fee_unknown")
    # Missing != false generally. This one default is explicitly documented by
    # ClobMarketDetails.itode in the official OpenAPI referenced by RULE.
    itode = compact.get("itode", False)
    require(type(itode) is bool, "venue_itode_unknown")
    seconds = market.get("seconds_delay")
    require(type(seconds) is int and 0 <= seconds <= 60, "venue_seconds_delay_unknown")
    reason = "sports_activation_or_combined_delay_unverified" if seconds else None
    if "oas" in compact:
        if type(compact["oas"]) is not int or compact["oas"] != 0:
            reason = "minimum_order_age_semantics_unverified"
    return {"state": "UNVERIFIED" if reason else "OBSERVED_SUPPORTED_TERMS", "reason": reason,
            "condition_id": condition, "token_ids": sorted(tokens),
            "minimum_order_shares": fstr(minimum), "minimum_order_units_source": "CLOB_BOOK_MIN_ORDER_SIZE",
            "tick_size": fstr(tick), "fee_rate": fstr(rate), "fee_exponent": fstr(exponent),
            "taker_only": True, "itode": itode,
            "itode_source": "EXPLICIT_BOOLEAN" if "itode" in compact else "DOCUMENTED_OPENAPI_OMISSION_FALSE",
            "configured_sports_delay_seconds": seconds,
            "mandatory_taker_delay_ms": None if reason else 250 if itode else 0,
            "pending_delay_cancelable": False,
            "fee_rounding_tie_verified": False, "fee_fill_fragmentation_verified": False,
            "fee_collection_asset_verified": False, "settlement_release_verified": False}


def collect(condition, tokens, fetch=public_get, wall_clock=time.time_ns, monotonic_clock=time.monotonic_ns):
    identity(condition, tokens)
    urls = [("compact", CLOB + "/clob-markets/" + condition), ("market", CLOB + "/markets/" + condition)]
    urls += [("book:" + token, CLOB + "/book?token_id=" + token) for token in sorted(tokens)]
    receipt = {"schema": SCHEMA, **SAFETY, "execution_authority": False,
        "scope": "NONATOMIC_PUBLIC_VENUE_METADATA_ONLY", "rule_version": RULE, "rule_sources": SOURCES,
        "condition_id": condition, "token_ids": sorted(tokens), "requests": [],
        "state": "UNVERIFIED", "reason": None, "terms": None,
        "venue_execution_verified": False, "valid_until_ns": None,
        "causal_session_binding_verified": False, "source_authenticity_cryptographically_attested": False}
    values = {}
    previous_end = 0
    try:
        for role, url in urls:
            start, mono_start = wall_clock(), monotonic_clock()
            entry = {"role": role, "url": url, "started_at_ns": start, "finished_at_ns": None,
                     "duration_monotonic_ns": None, "raw_response": None, "raw_sha256": None, "state": "FAILED"}
            receipt["requests"].append(entry)
            raw = fetch(url)
            end, mono_end = wall_clock(), monotonic_clock()
            entry.update(finished_at_ns=end, duration_monotonic_ns=mono_end-mono_start)
            require(previous_end <= start <= end and 0 <= mono_end-mono_start <= 10_000_000_000, "venue_collection_clock")
            # Reject material wall-clock jumps; these timestamps must never be
            # silently mapped into a native session clock domain.
            require(abs((end-start)-(mono_end-mono_start)) <= 10_000_000, "venue_collection_clock")
            previous_end = end
            value = decode(raw)
            entry.update(raw_response=raw.decode("utf-8"), raw_sha256=hashlib.sha256(raw).hexdigest(), state="RECEIVED")
            values[role] = value
        terms = project(condition, tokens, values["compact"], values["market"],
                        {token: values["book:"+token] for token in tokens})
        receipt.update(state=terms["state"], reason=terms["reason"], terms=terms)
    except (OSError, ValueError, RuntimeError, TypeError) as error:
        receipt["reason"] = type(error).__name__ + ":" + str(error)[:200]
    receipt["snapshot_sha256"] = sha(receipt)
    return receipt


def persist(directory, receipt):
    body = {k: v for k, v in receipt.items() if k != "snapshot_sha256"}
    digest = sha(body)
    require(receipt.get("snapshot_sha256") == digest, "venue_snapshot_digest")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (digest + ".json")
    data = json.dumps(receipt, sort_keys=True, indent=2, allow_nan=False) + "\n"
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
    except FileExistsError:
        require(not path.is_symlink() and path.read_text(encoding="utf-8") == data, "venue_immutable_conflict")
    return path


def validate_receipt(receipt):
    """Recompute supported projections from preserved bytes, not trusted flags.

    Returns the observed projection and conservative collection window. This
    authenticates internal consistency only, never HTTPS/source authenticity.
    """
    require(isinstance(receipt,dict) and receipt.get("schema")==SCHEMA and
            receipt.get("rule_version")==RULE and receipt.get("rule_sources")==SOURCES and
            all(receipt.get(k) is v for k,v in SAFETY.items()) and receipt.get("execution_authority") is False,
            "venue_receipt_identity")
    require(receipt.get("snapshot_sha256")==sha({k:v for k,v in receipt.items() if k!="snapshot_sha256"}),
            "venue_snapshot_digest")
    require(receipt.get("venue_execution_verified") is False and receipt.get("valid_until_ns") is None and
            receipt.get("causal_session_binding_verified") is False and
            receipt.get("source_authenticity_cryptographically_attested") is False and
            receipt.get("scope")=="NONATOMIC_PUBLIC_VENUE_METADATA_ONLY", "venue_receipt_scope")
    condition,tokens=receipt.get("condition_id"),receipt.get("token_ids")
    identity(condition,tokens)
    require(receipt.get("state")=="OBSERVED_SUPPORTED_TERMS" and receipt.get("reason") is None,
            "venue_terms_unavailable")
    rows=receipt.get("requests")
    expected=[("compact",CLOB+"/clob-markets/"+condition),("market",CLOB+"/markets/"+condition)]
    expected += [("book:"+t,CLOB+"/book?token_id="+t) for t in sorted(tokens)]
    require(isinstance(rows,list) and len(rows)==len(expected),"venue_receipt_requests")
    values={};start=None;end=0
    for row,(role,url) in zip(rows,expected):
        require(isinstance(row,dict) and row.get("role")==role and row.get("url")==url and
                row.get("state")=="RECEIVED","venue_receipt_request_identity")
        begin,finish,elapsed=(row.get(k) for k in ("started_at_ns","finished_at_ns","duration_monotonic_ns"))
        require(all(type(v) is int and 0<=v<2**63 for v in (begin,finish,elapsed)) and
                end<=begin<=finish and elapsed<=10_000_000_000 and
                abs(finish-begin-elapsed)<=10_000_000,"venue_receipt_clock")
        if start is None: start=begin
        end=finish
        raw=row.get("raw_response")
        require(isinstance(raw,str) and len(raw)<=MAX_RESPONSE,"venue_receipt_response")
        encoded=raw.encode()
        require(hashlib.sha256(encoded).hexdigest()==row.get("raw_sha256"),"venue_response_digest")
        values[role]=decode(encoded)
    terms=project(condition,tokens,values["compact"],values["market"],{t:values["book:"+t] for t in tokens})
    require(terms==receipt.get("terms") and terms["state"]=="OBSERVED_SUPPORTED_TERMS","venue_projection_mismatch")
    return terms,start,end


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--condition-id", required=True)
    parser.add_argument("--token-ids", required=True, nargs="+")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    receipt = collect(args.condition_id, args.token_ids)
    path = persist(args.output, receipt)
    print(json.dumps({"state": receipt["state"], "reason": receipt["reason"], "receipt": str(path),
                      "venue_execution_verified": False}, sort_keys=True))
    return 0 if receipt["state"] == "OBSERVED_SUPPORTED_TERMS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
