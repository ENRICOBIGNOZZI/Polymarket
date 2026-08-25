#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import os
import re
import time
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from v6_market_common import finite, parse_array, request_json
except ModuleNotFoundError:
    from scripts.v6_market_common import finite, parse_array, request_json

FIELDS = [
    "bundle_id", "strategy", "event_id", "created_ts", "mode", "expected_edge",
    "max_notional", "market_id", "side", "weight", "limit_price",
    "execution_deadline_ts", "hold_deadline_ts",
]
UP = re.compile(r"\b(above|over|exceed|exceeds|reach|reaches|at least|more than|higher than)\b", re.I)
DOWN = re.compile(r"\b(below|under|dip to|fall to|at most|less than|lower than)\b", re.I)
NUMBER = re.compile(r"(?P<prefix>[$€£]?)\s*(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<suffix>k|m|b|%|bp|bps)?", re.I)


@dataclass(frozen=True)
class Signature:
    family: str
    direction: str
    threshold: float
    unit: str


@dataclass
class Market:
    market_id: str
    question: str
    yes: str
    no: str
    liquidity: float
    end_ts: int


@dataclass
class Book:
    bid: float
    ask: float
    bid_size: float
    min_order: float


def parse_ts(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        raw = int(float(text))
        return raw // 1000 if raw > 10_000_000_000 else raw
    except ValueError:
        pass
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return int(parsed.timestamp())
    except ValueError:
        return 0


def parse_market(raw: dict[str, Any]) -> Market | None:
    ids = [str(x) for x in parse_array(raw.get("clobTokenIds"))]
    outcomes = [str(x).strip().lower() for x in parse_array(raw.get("outcomes"))]
    if len(ids) < 2:
        return None
    yi, ni = 0, 1
    for i, name in enumerate(outcomes[: len(ids)]):
        if name == "yes": yi = i
        elif name == "no": ni = i
    market_id = str(raw.get("id") or "")
    question = str(raw.get("question") or "").strip()
    if not market_id or not question:
        return None
    return Market(market_id, question, ids[yi], ids[ni],
                  max(0.0, finite(raw.get("liquidityNum"), finite(raw.get("liquidity"), 0.0))),
                  parse_ts(raw.get("endDate") or raw.get("endDateIso")))


def number_value(match: re.Match[str]) -> tuple[float, str]:
    value = float(match.group("num").replace(",", ""))
    prefix = (match.group("prefix") or "").strip()
    suffix = (match.group("suffix") or "").lower()
    unit = "count"
    if prefix:
        unit = {"$": "usd", "€": "eur", "£": "gbp"}.get(prefix, prefix)
    if suffix == "k": value *= 1e3
    elif suffix == "m": value *= 1e6
    elif suffix == "b": value *= 1e9
    elif suffix == "%": value /= 100.0; unit = "fraction"
    elif suffix in {"bp", "bps"}: value /= 10000.0; unit = "fraction"
    return value, unit


def threshold_signature(question: str) -> Signature | None:
    up = list(UP.finditer(question)); down = list(DOWN.finditer(question))
    if bool(up) == bool(down):
        return None
    direction = "UP" if up else "DOWN"
    direction_match = (up or down)[0]
    matches = list(NUMBER.finditer(question))
    if not matches:
        return None
    explicit = [m for m in matches if m.group("prefix") or m.group("suffix")]
    candidates = explicit or [m for m in matches if not (1900 <= float(m.group("num").replace(",", "")) <= 2100)]
    if not candidates:
        return None
    after = [m for m in candidates if m.start() >= direction_match.end()]
    chosen = min(after, key=lambda m: m.start() - direction_match.end()) if after else min(candidates, key=lambda m: abs(m.start() - direction_match.start()))
    threshold, unit = number_value(chosen)
    text = question.lower(); a, b = chosen.span(); text = text[:a] + " <threshold> " + text[b:]
    text = UP.sub(" <direction> ", text); text = DOWN.sub(" <direction> ", text)
    # Keep dates, years, entities and resolution wording. Only identical contracts
    # outside direction/threshold may enter the same payoff family.
    text = re.sub(r"[^a-z0-9<>%$€£:/.-]+", " ", text)
    family = re.sub(r"\s+", " ", text).strip()
    if not family or "<threshold>" not in family or "<direction>" not in family:
        return None
    return Signature(family, direction, threshold, unit)


def discover(gamma: str, limit: int, min_liquidity: float) -> list[Market]:
    output: list[Market] = []; offset = 0
    while len(output) < limit and offset < 5000:
        query = urllib.parse.urlencode({"active":"true","closed":"false","limit":100,"offset":offset,"order":"liquidityNum","ascending":"false"})
        root = request_json(gamma.rstrip("/") + "/markets?" + query)
        batch = root if isinstance(root, list) else root.get("markets", []) if isinstance(root, dict) else []
        if not batch: break
        for raw in batch:
            market = parse_market(raw) if isinstance(raw, dict) else None
            if market and market.liquidity >= min_liquidity: output.append(market)
            if len(output) >= limit: break
        if len(batch) < 100: break
        offset += 100
    return output


def fetch_books(clob: str, tokens: list[str]) -> dict[str, Book]:
    output: dict[str, Book] = {}
    for i in range(0, len(tokens), 80):
        root = request_json(clob.rstrip("/") + "/books", [{"token_id": token} for token in tokens[i:i+80]])
        for raw in root if isinstance(root, list) else []:
            if not isinstance(raw, dict): continue
            token = str(raw.get("asset_id") or ""); bids=[]; asks=[]
            for key, values in (("bids", bids), ("asks", asks)):
                for row in raw.get(key, []):
                    if not isinstance(row, dict): continue
                    price, size = finite(row.get("price")), finite(row.get("size"), 0.0)
                    if math.isfinite(price) and 0 < price < 1 and size > 0: values.append((price, size))
            bids.sort(reverse=True); asks.sort()
            if token and bids and asks:
                output[token] = Book(bids[0][0], asks[0][0], bids[0][1], max(1.0, finite(raw.get("min_order_size"), 1.0)))
    return output


def atomic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS); writer.writeheader(); writer.writerows(rows)
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail-closed typed threshold relation builder")
    parser.add_argument("--config", type=Path, default=Path("config/paper_v6.json")); parser.add_argument("--output", type=Path, required=True); parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--markets", type=int, default=700); parser.add_argument("--min-liquidity", type=float, default=10.0); parser.add_argument("--min-edge", type=float, default=0.0002); parser.add_argument("--max-trade-usd", type=float, default=60.0)
    args = parser.parse_args(); cfg = json.loads(args.config.read_text(encoding="utf-8")); gamma, clob = cfg["gamma_url"], cfg["clob_url"]; now = int(time.time())
    markets = discover(gamma, args.markets, args.min_liquidity)
    families: dict[tuple[str,str,str], list[tuple[float,Market]]] = defaultdict(list); parsed = 0
    for market in markets:
        signature = threshold_signature(market.question)
        if signature is None: continue
        parsed += 1; families[(signature.family, signature.direction, signature.unit)].append((signature.threshold, market))
    selected = {m.market_id:m for values in families.values() for _,m in values if len(values)>=2}
    tokens = [t for m in selected.values() for t in (m.yes,m.no)]; books = fetch_books(clob,tokens) if tokens else {}
    rows=[]; relations=0; bundles=0
    for (family,direction,unit),values in families.items():
        values.sort(key=lambda item:item[0])
        if len(values)<2: continue
        for (lo_t,lo),(hi_t,hi) in zip(values,values[1:]):
            if not hi_t>lo_t: continue
            relations += 1
            token_legs = [(lo,"YES",lo.yes),(hi,"NO",hi.no)] if direction=="UP" else [(hi,"YES",hi.yes),(lo,"NO",lo.no)]
            if any(token not in books for _,_,token in token_legs): continue
            leg_books=[books[token] for _,_,token in token_legs]; cost=sum(book.bid for book in leg_books); edge=1.0-cost
            if edge<=args.min_edge: continue
            min_shares=min(book.bid_size for book in leg_books); min_order=max(book.min_order for book in leg_books)
            if min_shares+1e-12<min_order: continue
            max_notional=min(args.max_trade_usd,min_shares*max(cost,1e-9))
            if max_notional<=0: continue
            identity=hashlib.sha256(f"{family}|{direction}|{unit}|{lo_t}|{hi_t}".encode()).hexdigest()[:16]
            bundle_id=f"STRUCTURAL_TYPED-{now}-{identity}"; end_ts=max(lo.end_ts,hi.end_ts); hold=max(now+3600,end_ts+3600 if end_ts else now+7*86400)
            for market,side,token in token_legs:
                rows.append({"bundle_id":bundle_id,"strategy":"STRUCTURAL_TYPED","event_id":"STRUCT_TYPED:"+identity,"created_ts":now,"mode":"MAKER","expected_edge":edge,"max_notional":max_notional,"market_id":market.market_id,"side":side,"weight":1.0,"limit_price":books[token].bid,"execution_deadline_ts":now+180,"hold_deadline_ts":hold})
            bundles += 1
    atomic_csv(args.output,rows)
    status={"timestamp":now,"paper_only":True,"markets":len(markets),"parsed_markets":parsed,"typed_families":sum(len(v)>=2 for v in families.values()),"relations_considered":relations,"bundles":bundles,"rows":len(rows),"best_edge":max((finite(r.get("expected_edge"),0.0) for r in rows),default=0.0),"parser_contract":"identical_text_outside_threshold_and_direction; dates/years retained","strategy":"STRUCTURAL_TYPED"}
    args.status.parent.mkdir(parents=True,exist_ok=True); tmp=args.status.with_suffix(args.status.suffix+".tmp"); tmp.write_text(json.dumps(status,indent=2,sort_keys=True)+"\n",encoding="utf-8"); os.replace(tmp,args.status)
    print(json.dumps(status,sort_keys=True)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
