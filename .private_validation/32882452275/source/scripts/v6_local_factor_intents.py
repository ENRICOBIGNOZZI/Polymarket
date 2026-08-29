#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import statistics
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FIELDS = [
    "bundle_id", "strategy", "event_id", "created_ts", "mode", "expected_edge",
    "max_notional", "market_id", "side", "weight", "limit_price",
    "execution_deadline_ts", "hold_deadline_ts",
]
THRESHOLD = re.compile(r"([$€£]?\s*\d[\d,]*(?:\.\d+)?\s*(?:k|m|b|%|bp|bps)?)", re.I)
DIRECTION = re.compile(r"\b(above|below|over|under|reach|exceed|dip|at least|at most|more than|less than)\b", re.I)
NORMAL = statistics.NormalDist()


def finite(value: Any, default=math.nan) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return x if math.isfinite(x) else default


def parse_array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            x = json.loads(value)
            return x if isinstance(x, list) else []
        except json.JSONDecodeError:
            return []
    return []


def request_json(url: str, payload: Any | None = None, timeout: int = 20) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"User-Agent":"polymarket-v6-paper/2","Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def logistic(z: float) -> float:
    if z >= 0:
        e = math.exp(-min(z, 40.0)); return 1.0 / (1.0 + e)
    e = math.exp(max(z, -40.0)); return e / (1.0 + e)


def logit(p: float) -> float:
    p = min(1.0 - 1e-6, max(1e-6, p)); return math.log(p / (1.0 - p))


def fee_per_share(price: float, rate: float, exponent: float) -> float:
    if not 0.0 < price < 1.0 or rate <= 0.0:
        return 0.0
    return rate * (price * (1.0 - price)) ** max(0.0, exponent)


@dataclass
class Market:
    market_id: str
    event_id: str
    question: str
    yes: str
    no: str
    liquidity: float


@dataclass
class Book:
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    min_order: float

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)

    @property
    def spread(self) -> float:
        return max(0.0, self.ask - self.bid)


@dataclass
class Candidate:
    cluster: str
    market: Market
    residual_z: float
    phi: float
    tstat: float
    pvalue: float
    loading: float
    yes_sd: float
    expected_residual_change: float
    common_points: int


def parse_market(raw: dict[str, Any]) -> Market | None:
    ids = [str(x) for x in parse_array(raw.get("clobTokenIds"))]
    outcomes = [str(x).lower() for x in parse_array(raw.get("outcomes"))]
    if len(ids) < 2:
        return None
    yi, ni = 0, 1
    for i, x in enumerate(outcomes[:len(ids)]):
        if x == "yes": yi = i
        elif x == "no": ni = i
    market_id = str(raw.get("id") or "")
    question = str(raw.get("question") or "")
    if not market_id or not question:
        return None
    event = str(raw.get("eventId") or "")
    events = raw.get("events")
    if not event and isinstance(events, list) and events and isinstance(events[0], dict):
        event = str(events[0].get("id") or "")
    return Market(
        market_id,
        event or str(raw.get("conditionId") or market_id),
        question,
        ids[yi], ids[ni],
        max(0.0, finite(raw.get("liquidityNum"), 0.0)),
    )


def discover(gamma: str, limit: int, min_liq: float) -> list[Market]:
    out: list[Market] = []
    offset = 0
    while len(out) < limit and offset < 5000:
        params = urllib.parse.urlencode({"active":"true","closed":"false","limit":100,"offset":offset,"order":"liquidityNum","ascending":"false"})
        raw = request_json(f"{gamma}/markets?{params}")
        batch = raw if isinstance(raw, list) else raw.get("markets", []) if isinstance(raw, dict) else []
        if not batch:
            break
        for row in batch:
            m = parse_market(row) if isinstance(row, dict) else None
            if m and m.liquidity >= min_liq:
                out.append(m)
            if len(out) >= limit:
                break
        if len(batch) < 100:
            break
        offset += 100
    return out


def payoff_family(question: str) -> str | None:
    if not THRESHOLD.search(question) or not DIRECTION.search(question):
        return None
    x = question.lower()
    x = THRESHOLD.sub(" <threshold> ", x)
    x = DIRECTION.sub(" <direction> ", x)
    x = re.sub(r"\b20\d{2}\b", " <year> ", x)
    x = re.sub(r"[^a-z<>]+", " ", x)
    return re.sub(r"\s+", " ", x).strip()


def clusters(markets: list[Market], max_clusters: int) -> list[tuple[str, list[Market]]]:
    groups: dict[str, list[Market]] = defaultdict(list)
    for m in markets:
        groups["event:" + m.event_id].append(m)
        fam = payoff_family(m.question)
        if fam:
            groups["payoff:" + fam].append(m)
    candidates = [(k, v) for k, v in groups.items() if 3 <= len(v) <= 25]
    candidates.sort(key=lambda kv: sum(x.liquidity for x in kv[1]), reverse=True)
    seen: set[tuple[str, ...]] = set()
    out: list[tuple[str, list[Market]]] = []
    for key, ms in candidates:
        ids = tuple(sorted(m.market_id for m in ms))
        if ids in seen:
            continue
        seen.add(ids)
        out.append((key, ms))
        if len(out) >= max_clusters:
            break
    return out


def fetch_books(clob: str, markets: list[Market]) -> dict[str, Book]:
    tokens = [t for m in markets for t in (m.yes, m.no)]
    out: dict[str, Book] = {}
    for i in range(0, len(tokens), 80):
        raw = request_json(clob + "/books", [{"token_id": x} for x in tokens[i:i+80]])
        for row in raw if isinstance(raw, list) else []:
            if not isinstance(row, dict):
                continue
            token = str(row.get("asset_id") or "")
            bids: list[tuple[float,float]] = []
            asks: list[tuple[float,float]] = []
            for z in row.get("bids", []):
                if isinstance(z, dict):
                    p, q = finite(z.get("price")), finite(z.get("size"), 0.0)
                    if math.isfinite(p) and 0 < p < 1 and q > 0: bids.append((p, q))
            for z in row.get("asks", []):
                if isinstance(z, dict):
                    p, q = finite(z.get("price")), finite(z.get("size"), 0.0)
                    if math.isfinite(p) and 0 < p < 1 and q > 0: asks.append((p, q))
            if token and bids and asks:
                bids.sort(reverse=True); asks.sort()
                out[token] = Book(bids[0][0], asks[0][0], bids[0][1], asks[0][1], max(1.0, finite(row.get("min_order_size"), 1.0)))
    return out


def parse_history(rows: list[Any], fidelity: int) -> dict[int, float]:
    bucket = fidelity * 60
    out: dict[int, float] = {}
    for z in rows:
        if not isinstance(z, dict):
            continue
        t = int(finite(z.get("t"), 0)); p = finite(z.get("p"))
        if t > 0 and math.isfinite(p) and 0 < p < 1:
            out[(t // bucket) * bucket] = logit(p)
    return out


def fetch_histories(clob: str, token_by_market: dict[str, str], start: int, end: int, fidelity: int) -> tuple[dict[str, dict[int,float]], list[str]]:
    token_to_market = {token: mid for mid, token in token_by_market.items()}
    tokens = list(token_to_market)
    out: dict[str, dict[int,float]] = {}
    failures: list[str] = []
    for i in range(0, len(tokens), 20):
        batch = tokens[i:i+20]
        try:
            raw = request_json(clob + "/batch-prices-history", {"markets": batch, "start_ts": start, "end_ts": end, "fidelity": fidelity})
            hist = raw.get("history", {}) if isinstance(raw, dict) else {}
            if isinstance(hist, dict):
                for token, rows in hist.items():
                    if token in token_to_market and isinstance(rows, list):
                        parsed = parse_history(rows, fidelity)
                        if parsed:
                            out[token_to_market[token]] = parsed
        except Exception as exc:
            failures.append(f"batch:{type(exc).__name__}")
    # Bounded fallback for missing high-priority assets only; avoids API storms.
    missing = [(mid, token) for mid, token in token_by_market.items() if mid not in out][:20]
    for mid, token in missing:
        try:
            url = f"{clob}/prices-history?market={urllib.parse.quote(token)}&startTs={start}&endTs={end}&fidelity={fidelity}"
            raw = request_json(url)
            parsed = parse_history(raw.get("history", []) if isinstance(raw, dict) else [], fidelity)
            if parsed:
                out[mid] = parsed
        except Exception as exc:
            failures.append(f"single:{mid}:{type(exc).__name__}")
    return out, failures


def ar_fit(resid: list[float]) -> tuple[float, float, float, float]:
    if len(resid) < 30:
        return 1.0, 0.0, 0.0, 0.0
    mu = statistics.fmean(resid)
    sd = statistics.stdev(resid)
    if sd < 1e-6:
        return 1.0, 0.0, mu, sd
    lag = resid[:-1]
    dr = [resid[i] - resid[i-1] for i in range(1, len(resid))]
    ml, md = statistics.fmean(lag), statistics.fmean(dr)
    sxx = sum((x - ml) ** 2 for x in lag)
    sxy = sum((x - ml) * (y - md) for x, y in zip(lag, dr))
    if sxx < 1e-10:
        return 1.0, 0.0, mu, sd
    gamma = sxy / sxx
    c = md - gamma * ml
    rss = sum((y - (c + gamma * x)) ** 2 for x, y in zip(lag, dr))
    sigma2 = rss / max(1, len(lag) - 2)
    se = math.sqrt(max(0.0, sigma2) / sxx)
    t = gamma / se if se > 1e-12 else 0.0
    return 1.0 + gamma, t, mu, sd


def pvalue_from_t(tstat: float) -> float:
    return min(1.0, max(0.0, 2.0 * (1.0 - NORMAL.cdf(abs(tstat)))))


def bh_cutoff(pvalues: list[float], q: float) -> float:
    if not pvalues:
        return 0.0
    ordered = sorted(p for p in pvalues if math.isfinite(p))
    m = len(ordered)
    cutoff = 0.0
    for i, p in enumerate(ordered, start=1):
        if p <= q * i / m:
            cutoff = p
    return cutoff


def local_candidates(key: str, ms: list[Market], series: dict[str,dict[int,float]], min_common: int, min_z: float) -> list[Candidate]:
    usable = [m for m in ms if m.market_id in series and len(series[m.market_id]) >= min_common]
    if len(usable) < 3:
        return []
    common = set(series[usable[0].market_id])
    for m in usable[1:]:
        common &= set(series[m.market_id])
    times = sorted(common)
    if len(times) < min_common:
        return []
    raw = {m.market_id: [series[m.market_id][t] for t in times] for m in usable}
    standardized: dict[str, list[float]] = {}
    scales: dict[str, float] = {}
    for m in usable:
        y = raw[m.market_id]
        mu = statistics.fmean(y); sd = statistics.stdev(y)
        if sd <= 1e-6:
            continue
        standardized[m.market_id] = [(x - mu) / sd for x in y]
        scales[m.market_id] = sd
    usable = [m for m in usable if m.market_id in standardized]
    if len(usable) < 3:
        return []
    factor = [statistics.fmean(standardized[m.market_id][j] for m in usable) for j in range(len(times))]
    fm = statistics.fmean(factor)
    fvar = sum((x - fm) ** 2 for x in factor)
    if fvar <= 1e-8:
        return []
    out: list[Candidate] = []
    for m in usable:
        zseries = standardized[m.market_id]
        zm = statistics.fmean(zseries)
        loading = sum((x-zm)*(f-fm) for x,f in zip(zseries,factor)) / fvar
        if abs(loading) < 0.05:
            continue
        resid = [x - loading*f for x,f in zip(zseries,factor)]
        phi, tstat, rmu, rsd = ar_fit(resid)
        if not (0.02 < phi < 0.999 and tstat < 0.0 and rsd > 0.0):
            continue
        rz = (resid[-1] - rmu) / rsd
        if abs(rz) < min_z:
            continue
        out.append(Candidate(key, m, rz, phi, tstat, pvalue_from_t(tstat), loading, scales[m.market_id], (phi - 1.0) * (resid[-1] - rmu), len(times)))
    return out


def build_pair_intent(key: str, signals: list[Candidate], books: dict[str,Book], now: int, min_edge: float, max_trade: float, fee_rate: float, fee_exp: float, slip_bps: float, serial: int) -> list[dict[str,Any]]:
    best: tuple[float, Candidate, Candidate] | None = None
    for i, a in enumerate(signals):
        for b in signals[i+1:]:
            if a.residual_z * b.residual_z >= 0.0:
                continue
            side_a = "NO" if a.residual_z > 0 else "YES"
            side_b = "NO" if b.residual_z > 0 else "YES"
            sign_a = -1.0 if side_a == "NO" else 1.0
            sign_b = -1.0 if side_b == "NO" else 1.0
            exposure_a = sign_a * a.loading
            exposure_b = sign_b * b.loading
            if exposure_a * exposure_b >= 0.0 or abs(exposure_b) < 1e-6:
                continue
            score = abs(a.residual_z * a.tstat) + abs(b.residual_z * b.tstat)
            if best is None or score > best[0]:
                best = (score, a, b)
    if best is None:
        return []
    _, a, b = best
    side_a = "NO" if a.residual_z > 0 else "YES"
    side_b = "NO" if b.residual_z > 0 else "YES"
    sign_a = -1.0 if side_a == "NO" else 1.0
    sign_b = -1.0 if side_b == "NO" else 1.0
    weight_a = 1.0
    weight_b = abs((sign_a * a.loading) / (sign_b * b.loading))
    if not 0.05 <= weight_b <= 10.0:
        return []
    legs = [(a, side_a, weight_a), (b, side_b, weight_b)]
    capital = 0.0; expected_pnl = 0.0; units = math.inf; min_units = 0.0
    slip = max(0.0, slip_bps) / 10000.0
    half_lives: list[float] = []
    for sig, side, weight in legs:
        yb = books.get(sig.market.yes); nb = books.get(sig.market.no)
        if yb is None or nb is None:
            return []
        side_book = yb if side == "YES" else nb
        yes_mid = yb.mid
        yes_logit_move = sig.expected_residual_change * sig.yes_sd
        future_yes = logistic(logit(yes_mid) + yes_logit_move)
        future_side = future_yes if side == "YES" else 1.0 - future_yes
        exit_px = max(0.001, min(0.999, future_side - 0.5 * side_book.spread)) * (1.0 - slip)
        fee = fee_per_share(exit_px, fee_rate, fee_exp)
        pnl_per_share = exit_px - fee - side_book.bid
        if pnl_per_share <= 0.0:
            return []
        capital += weight * side_book.bid
        expected_pnl += weight * pnl_per_share
        units = min(units, side_book.bid_size / weight)
        min_units = max(min_units, side_book.min_order / weight)
        if 0.0 < sig.phi < 1.0:
            half_lives.append(-math.log(2.0) / math.log(sig.phi))
    edge = expected_pnl / max(capital, 1e-9)
    if edge <= min_edge or not math.isfinite(units) or units + 1e-12 < min_units:
        return []
    max_notional = min(max_trade, units * capital)
    if max_notional <= 0.0:
        return []
    bars = max(1.0, min(24.0, 2.0 * max(half_lives, default=2.0)))
    hold = now + int(bars * 3600)
    deadline = now + 180
    bundle = f"LOCAL_FACTOR-{now}-{serial}"
    rows: list[dict[str,Any]] = []
    for sig, side, weight in legs:
        book = books[sig.market.yes if side == "YES" else sig.market.no]
        rows.append({
            "bundle_id":bundle, "strategy":"LOCAL_FACTOR", "event_id":key,
            "created_ts":now, "mode":"MAKER", "expected_edge":edge, "max_notional":max_notional,
            "market_id":sig.market.market_id, "side":side, "weight":weight, "limit_price":book.bid,
            "execution_deadline_ts":deadline, "hold_deadline_ts":hold,
        })
    return rows


def atomic_csv(path: Path, rows: list[dict[str,Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
    with tmp.open("w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=FIELDS); w.writeheader(); w.writerows(rows)
    os.replace(tmp, path)


def main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--config",type=Path,default=Path("config/paper_v6.json")); ap.add_argument("--output",type=Path,required=True); ap.add_argument("--status",type=Path,required=True)
    ap.add_argument("--markets",type=int,default=400); ap.add_argument("--min-liquidity",type=float,default=10); ap.add_argument("--lookback-hours",type=int,default=336); ap.add_argument("--fidelity-minutes",type=int,default=60)
    ap.add_argument("--max-clusters",type=int,default=15); ap.add_argument("--min-common-points",type=int,default=48); ap.add_argument("--min-z",type=float,default=1.0); ap.add_argument("--fdr",type=float,default=.10)
    ap.add_argument("--min-edge",type=float,default=.0002); ap.add_argument("--max-trade-usd",type=float,default=60); ap.add_argument("--slippage-bps",type=float,default=5); args=ap.parse_args()
    cfg=json.loads(args.config.read_text()); gamma,clob=cfg["gamma_url"],cfg["clob_url"]; now=int(time.time()); failures: list[str]=[]; rows: list[dict[str,Any]]=[]; serial=0
    fee_rate=max(0.0,finite((cfg.get("v6") or {}).get("assumed_fee_rate"),.07)); fee_exp=max(0.0,finite((cfg.get("v6") or {}).get("assumed_fee_exponent"),1.0))
    try:
        ms=discover(gamma,args.markets,args.min_liquidity); cs=clusters(ms,args.max_clusters); selected={m.market_id:m for _,group in cs for m in group}; books=fetch_books(clob,list(selected.values()))
    except Exception as exc:
        ms=[]; cs=[]; selected={}; books={}; failures.append(f"market_data:{type(exc).__name__}:{exc}")
    start=now-args.lookback_hours*3600
    series,hfail=fetch_histories(clob,{m.market_id:m.yes for m in selected.values()},start,now,args.fidelity_minutes) if selected else ({},[])
    failures.extend(hfail[:30])
    all_candidates: list[Candidate] = []
    by_cluster: dict[str,list[Candidate]] = defaultdict(list)
    for key,group in cs:
        cands=local_candidates(key,group,series,args.min_common_points,args.min_z)
        all_candidates.extend(cands); by_cluster[key].extend(cands)
    cutoff=bh_cutoff([c.pvalue for c in all_candidates],max(1e-4,min(.5,args.fdr)))
    eligible={id(c) for c in all_candidates if cutoff>0.0 and c.pvalue<=cutoff}
    for key,cands in by_cluster.items():
        selected_cands=[c for c in cands if id(c) in eligible]
        intent=build_pair_intent(key,selected_cands,books,now,args.min_edge,args.max_trade_usd,fee_rate,fee_exp,args.slippage_bps,serial)
        if intent:
            rows.extend(intent); serial+=1
    atomic_csv(args.output,rows)
    status={
        "timestamp":now,"paper_only":True,"markets":len(ms),"clusters":len(cs),"histories":len(series),"common_sample_min":args.min_common_points,
        "reversion_tests":len(all_candidates),"fdr":args.fdr,"bh_pvalue_cutoff":cutoff,"fdr_eligible_signals":len(eligible),
        "bundles":serial,"intent_rows":len(rows),"best_edge":max((float(r["expected_edge"]) for r in rows),default=0.0),"failures":failures,
    }
    args.status.parent.mkdir(parents=True,exist_ok=True)
    tmp=args.status.with_name(args.status.name+f".tmp.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(status,indent=2,sort_keys=True)+"\n")
    os.replace(tmp,args.status)
    print(json.dumps(status,sort_keys=True)); return 0


if __name__=="__main__": raise SystemExit(main())
