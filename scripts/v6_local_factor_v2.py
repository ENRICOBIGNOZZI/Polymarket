#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

try:
    import v6_local_factor_intents as base
    from v6_market_common import TapeFlow, fee_per_share, fill_probability_proxy, finite, request_json, resolve_fee_details
except ModuleNotFoundError:
    from scripts import v6_local_factor_intents as base
    from scripts.v6_market_common import TapeFlow, fee_per_share, fill_probability_proxy, finite, request_json, resolve_fee_details


def ar_phi(residual: list[float]) -> tuple[float, float, float]:
    if len(residual) < 30:
        return 1.0, 0.0, 0.0
    mean = statistics.fmean(residual)
    lag, future = residual[:-1], residual[1:]
    lag_mean, future_mean = statistics.fmean(lag), statistics.fmean(future)
    sxx = sum((x - lag_mean) ** 2 for x in lag)
    if sxx <= 1e-12:
        return 1.0, mean, 0.0
    phi = sum((x - lag_mean) * (y - future_mean) for x, y in zip(lag, future)) / sxx
    return phi, mean, statistics.stdev(residual)


def mean_reversion_score_pvalue(residual: list[float], seed: int, reps: int = 600) -> tuple[float, float]:
    """Circular-block one-sided test of E[(r[t-1]-mu)*Delta r[t]] < 0."""
    if len(residual) < 32:
        return 1.0, 0.0
    mu = statistics.fmean(residual)
    scores = [(residual[i - 1] - mu) * (residual[i] - residual[i - 1]) for i in range(1, len(residual))]
    observed = statistics.fmean(scores)
    centered = [value - observed for value in scores]
    n = len(centered); block = max(2, min(n, int(round(math.sqrt(n))))); rng = random.Random(seed); less_equal = 0
    for _ in range(reps):
        sample: list[float] = []
        while len(sample) < n:
            start = rng.randrange(n)
            sample.extend(centered[(start + j) % n] for j in range(block))
        if statistics.fmean(sample[:n]) <= observed:
            less_equal += 1
    return (less_equal + 1) / (reps + 1), observed


def local_candidates(key: str, markets: list[Any], series: dict[str, dict[int, float]], min_common: int, min_z: float) -> list[Any]:
    usable = [m for m in markets if m.market_id in series and len(series[m.market_id]) >= min_common]
    if len(usable) < 3:
        return []
    common = set(series[usable[0].market_id])
    for market in usable[1:]: common &= set(series[market.market_id])
    times = sorted(common)
    if len(times) < min_common: return []
    raw = {m.market_id: [series[m.market_id][ts] for ts in times] for m in usable}
    standardized: dict[str, list[float]] = {}; scales: dict[str, float] = {}
    for market in usable:
        values = raw[market.market_id]; sd = statistics.stdev(values)
        if sd <= 1e-6: continue
        mean = statistics.fmean(values); standardized[market.market_id] = [(value - mean) / sd for value in values]; scales[market.market_id] = sd
    usable = [m for m in usable if m.market_id in standardized]
    output = []
    for market in usable:
        others = [other for other in usable if other.market_id != market.market_id]
        factor = [statistics.fmean(standardized[other.market_id][j] for other in others) for j in range(len(times))]
        fm = statistics.fmean(factor); fvar = sum((x - fm) ** 2 for x in factor)
        if fvar <= 1e-8: continue
        values = standardized[market.market_id]; vm = statistics.fmean(values)
        loading = sum((x - vm) * (f - fm) for x, f in zip(values, factor)) / fvar
        if abs(loading) < 0.05: continue
        residual = [x - loading * f for x, f in zip(values, factor)]
        phi, rmu, rsd = ar_phi(residual)
        if not (0.02 < phi < 0.999 and rsd > 0.0): continue
        residual_z = (residual[-1] - rmu) / rsd
        if abs(residual_z) < min_z: continue
        pvalue, score_mean = mean_reversion_score_pvalue(residual, 20260825 + sum(ord(c) for c in key + market.market_id))
        # Keep the incumbent Candidate schema; tstat is now a signed diagnostic score,
        # never interpreted as a Normal/DF p-value.
        output.append(base.Candidate(key, market, residual_z, phi, score_mean, pvalue, loading, scales[market.market_id], (phi - 1.0) * (residual[-1] - rmu), len(times)))
    return output


def raw_market(gamma: str, market_id: str, cache: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if market_id in cache: return cache[market_id]
    try:
        value = request_json(f"{gamma.rstrip('/')}/markets/{market_id}")
    except Exception:
        return None
    if isinstance(value, dict): cache[market_id] = value; return value
    return None


def build_pair(key: str, signals: list[Any], books: dict[str, Any], flow: TapeFlow, *, gamma: str, clob: str, now: int, min_edge: float, max_trade: float, slippage_bps: float, flow_lookback: int, min_fill_probability: float, serial: int, cache: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    best = None
    for i, a in enumerate(signals):
        for b in signals[i + 1:]:
            if a.residual_z * b.residual_z >= 0.0: continue
            side_a = "NO" if a.residual_z > 0 else "YES"; side_b = "NO" if b.residual_z > 0 else "YES"
            sign_a = -1.0 if side_a == "NO" else 1.0; sign_b = -1.0 if side_b == "NO" else 1.0
            if sign_a * a.loading * sign_b * b.loading >= 0.0 or abs(sign_b * b.loading) < 1e-8: continue
            weight_b = abs((sign_a * a.loading) / (sign_b * b.loading))
            if not 0.05 <= weight_b <= 10.0: continue
            legs = [(a, side_a, 1.0), (b, side_b, weight_b)]; capital = expected = 0.0; fill_probs=[]; valid=True
            for signal, side, weight in legs:
                yes_book, no_book = books.get(signal.market.yes), books.get(signal.market.no)
                if yes_book is None or no_book is None: valid=False; break
                book = yes_book if side == "YES" else no_book; token = signal.market.yes if side == "YES" else signal.market.no
                market_raw = raw_market(gamma, signal.market.market_id, cache)
                if market_raw is None: valid=False; break
                fee = resolve_fee_details(market_raw, clob, str(market_raw.get("conditionId") or ""), token)
                if not fee.verified: valid=False; break
                future_yes = base.logistic(base.logit(yes_book.mid) + signal.expected_residual_change * signal.yes_sd)
                future_side = future_yes if side == "YES" else 1.0 - future_yes
                exit_price = max(0.001, min(0.999, future_side - 0.5 * book.spread)) * (1.0 - max(0.0, slippage_bps) / 10000.0)
                pnl = exit_price - fee_per_share(exit_price, fee, taker=True) - book.bid
                if pnl <= 0.0: valid=False; break
                capital += weight * book.bid; expected += weight * pnl
                rate = flow.compatible_sell_rate(token, book.bid, lookback_seconds=flow_lookback)
                fill_probs.append(fill_probability_proxy(queue_ahead=book.bid_size, own_shares=book.min_order, compatible_flow_per_second=rate, horizon_seconds=180, prior_flow_per_second=1.0/300.0))
            if not valid or capital <= 0: continue
            edge = expected / capital; fill_proxy = min(fill_probs, default=0.0); utility = edge * fill_proxy
            if edge > min_edge and fill_proxy >= min_fill_probability and (best is None or (utility, edge) > (best[0], best[1])):
                best = (utility, edge, a, b)
    if best is None: return [], "no_fillable_pair"
    _, edge, a, b = best; side_a = "NO" if a.residual_z > 0 else "YES"; side_b = "NO" if b.residual_z > 0 else "YES"; sign_a = -1.0 if side_a == "NO" else 1.0; sign_b = -1.0 if side_b == "NO" else 1.0; weight_b = abs((sign_a * a.loading)/(sign_b * b.loading)); legs=[(a,side_a,1.0),(b,side_b,weight_b)]
    capital=sum(weight*books[signal.market.yes if side=="YES" else signal.market.no].bid for signal,side,weight in legs); units=math.inf; min_units=0.0; half_lives=[]
    for signal,side,weight in legs:
        book=books[signal.market.yes if side=="YES" else signal.market.no]; units=min(units,book.bid_size/weight); min_units=max(min_units,book.min_order/weight); half_lives.append(-math.log(2.0)/math.log(signal.phi))
    if not math.isfinite(units) or units+1e-12<min_units: return [], "depth"
    max_notional=min(max_trade,units*capital)
    if max_notional<=0: return [], "notional"
    hold=now+int(max(1.0,min(24.0,2.0*max(half_lives,default=2.0)))*3600); bundle=f"LOCAL_FACTOR-{now}-{serial}"; rows=[]
    for signal,side,weight in legs:
        book=books[signal.market.yes if side=="YES" else signal.market.no]; rows.append({"bundle_id":bundle,"strategy":"LOCAL_FACTOR","event_id":key,"created_ts":now,"mode":"MAKER","expected_edge":edge,"max_notional":max_notional,"market_id":signal.market.market_id,"side":side,"weight":weight,"limit_price":book.bid,"execution_deadline_ts":now+180,"hold_deadline_ts":hold})
    return rows, "accepted"


def main() -> int:
    parser=argparse.ArgumentParser(); parser.add_argument("--config",type=Path,default=Path("config/paper_v6.json")); parser.add_argument("--output",type=Path,required=True); parser.add_argument("--status",type=Path,required=True); parser.add_argument("--trade-tape",type=Path); parser.add_argument("--markets",type=int,default=400); parser.add_argument("--min-liquidity",type=float,default=10); parser.add_argument("--lookback-hours",type=int,default=336); parser.add_argument("--fidelity-minutes",type=int,default=60); parser.add_argument("--max-clusters",type=int,default=15); parser.add_argument("--min-common-points",type=int,default=48); parser.add_argument("--min-z",type=float,default=1.0); parser.add_argument("--fdr",type=float,default=.10); parser.add_argument("--min-edge",type=float,default=.0002); parser.add_argument("--max-trade-usd",type=float,default=60); parser.add_argument("--slippage-bps",type=float,default=5); parser.add_argument("--flow-lookback-seconds",type=int,default=900); parser.add_argument("--min-fill-probability",type=float,default=0.03); args=parser.parse_args()
    cfg=json.loads(args.config.read_text(encoding="utf-8")); gamma,clob=cfg["gamma_url"],cfg["clob_url"]; now=int(time.time()); failures=[]; rows=[]; rejections=Counter(); cache={}
    try: markets=base.discover(gamma,args.markets,args.min_liquidity); cluster_rows=base.clusters(markets,args.max_clusters); selected={m.market_id:m for _,group in cluster_rows for m in group}; books=base.fetch_books(clob,list(selected.values()))
    except Exception as exc: markets,cluster_rows,selected,books=[],[],{},{}; failures.append(f"market_data:{type(exc).__name__}:{exc}")
    history,hfail=base.fetch_histories(clob,{market_id:market.yes for market_id,market in selected.items()},now-args.lookback_hours*3600,now,args.fidelity_minutes) if selected else ({},[]); failures.extend(hfail[:50]); all_candidates=[]; by_cluster=defaultdict(list)
    for key,group in cluster_rows:
        candidates=local_candidates(key,group,history,args.min_common_points,args.min_z); all_candidates.extend(candidates); by_cluster[key].extend(candidates)
    cutoff=base.bh_cutoff([candidate.pvalue for candidate in all_candidates],max(1e-4,min(.5,args.fdr))); eligible={id(candidate) for candidate in all_candidates if cutoff>0 and candidate.pvalue<=cutoff}; flow=TapeFlow.from_csv(args.trade_tape or (args.output.parent/"trade_tape.csv"),lookback_seconds=args.flow_lookback_seconds,now=now); serial=0
    for key,candidates in by_cluster.items():
        selected_candidates=[candidate for candidate in candidates if id(candidate) in eligible]; intent,reason=build_pair(key,selected_candidates,books,flow,gamma=gamma,clob=clob,now=now,min_edge=args.min_edge,max_trade=args.max_trade_usd,slippage_bps=args.slippage_bps,flow_lookback=args.flow_lookback_seconds,min_fill_probability=args.min_fill_probability,serial=serial,cache=cache)
        if intent: rows.extend(intent); serial+=1
        elif selected_candidates: rejections[reason]+=1
    base.atomic_csv(args.output,rows); status={"timestamp":now,"paper_only":True,"markets":len(markets),"clusters":len(cluster_rows),"history_markets":len(history),"candidate_count":len(all_candidates),"fdr_cutoff":cutoff,"fdr_survivors":sum(id(candidate) in eligible for candidate in all_candidates),"bundles":serial,"rows":len(rows),"best_edge":max((finite(row.get("expected_edge"),0.0) for row in rows),default=0.0),"min_fill_probability":args.min_fill_probability,"flow_lookback_seconds":args.flow_lookback_seconds,"rejections":dict(sorted(rejections.items())),"failures":failures[:50],"test":"circular_block_bootstrap_negative_reversion_score","factor":"leave_one_out_local_cross_section"}; args.status.parent.mkdir(parents=True,exist_ok=True); tmp=args.status.with_suffix(args.status.suffix+".tmp"); tmp.write_text(json.dumps(status,indent=2,sort_keys=True)+"\n",encoding="utf-8"); os.replace(tmp,args.status); print(json.dumps(status,sort_keys=True)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
