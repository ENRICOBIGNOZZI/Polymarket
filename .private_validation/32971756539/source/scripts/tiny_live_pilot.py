#!/usr/bin/env python3
"""Tiny, explicitly gated real-money Polymarket pilot.

Default mode is DRY RUN and does not import the authenticated SDK. Real order
submission requires all of:
  * a walk-forward report whose OOS gate passed;
  * an explicit --execute flag;
  * POLYMARKET_LIVE_ENABLE=I_UNDERSTAND_REAL_MONEY;
  * credentials supplied only through environment variables.

The pilot admits at most one multi-leg maker bundle, caps total and per-leg
notional, submits post-only GTC buys, cancels residual orders, and attempts FOK
unwind of every matched leg after completion/timeout. It is intentionally too
small and too conservative to be a production execution engine.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

CONSENT = "I_UNDERSTAND_REAL_MONEY"
REQUIRED_CREDS = ("PK", "CLOB_API_KEY", "CLOB_SECRET", "CLOB_PASS_PHRASE")


def http_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "polymarket-v4-tiny-pilot/1"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def decode_array(v):
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str):
        try:
            x = json.loads(v)
            return [str(y) for y in x] if isinstance(x, list) else []
        except Exception:
            return []
    return []


def load_report(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_bundles(path: Path, now: int):
    grouped = defaultdict(list)
    with path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                if r.get("mode") != "MAKER" or int(r["execution_deadline_ts"]) <= now:
                    continue
                r["expected_edge_f"] = float(r["expected_edge"])
                r["weight_f"] = float(r["weight"])
                r["max_notional_f"] = float(r["max_notional"])
                r["limit_price_f"] = float(r["limit_price"])
                grouped[r["bundle_id"]].append(r)
            except (KeyError, ValueError, TypeError):
                continue
    return grouped


def choose_bundle(grouped, threshold: float):
    candidates = []
    for bid, rows in grouped.items():
        if len(rows) < 2 or len({(x["market_id"], x["side"]) for x in rows}) != len(rows):
            continue
        if any(x["expected_edge_f"] < threshold or x["weight_f"] <= 0 for x in rows):
            continue
        candidates.append(rows)
    candidates.sort(key=lambda x: (x[0]["expected_edge_f"], int(x[0]["created_ts"])), reverse=True)
    return candidates[0] if candidates else None


def market_tokens(gamma_url: str, market_id: str):
    m = http_json(f"{gamma_url.rstrip('/')}/markets/{urllib.parse.quote(market_id)}")
    toks, outcomes = decode_array(m.get("clobTokenIds")), decode_array(m.get("outcomes"))
    if len(toks) < 2:
        raise RuntimeError(f"market {market_id}: missing CLOB token ids")
    mapping = {o.upper(): toks[i] for i, o in enumerate(outcomes[:len(toks)])}
    return mapping.get("YES", toks[0]), mapping.get("NO", toks[1])


def parse_book(raw):
    def levels(name):
        out = []
        for x in raw.get(name, []) or []:
            try:
                out.append((float(x["price"]), float(x["size"])))
            except Exception:
                pass
        return out
    bids, asks = levels("bids"), levels("asks")
    if not bids or not asks:
        raise RuntimeError("empty/crossed book")
    bb = max(p for p, _ in bids)
    ba = min(p for p, _ in asks)
    touch = sum(q for p, q in bids if abs(p - bb) <= 1e-12)
    tick = float(raw.get("tick_size") or raw.get("tickSize") or 0.01)
    min_order = float(raw.get("min_order_size") or raw.get("minOrderSize") or 1.0)
    return bb, ba, touch, tick, min_order


def preflight(rows, gamma_url: str, clob_url: str, max_bundle_usd: float, max_leg_usd: float, queue_fraction: float):
    legs = []
    capital_per_unit = 0.0
    for r in rows:
        yes, no = market_tokens(gamma_url, r["market_id"])
        token = yes if r["side"] == "YES" else no
        raw = http_json(f"{clob_url.rstrip('/')}/book?token_id={urllib.parse.quote(token)}")
        bb, ba, touch, tick, min_order = parse_book(raw)
        limit = r["limit_price_f"] if r["limit_price_f"] > 0 else bb
        # The pilot only posts; never cross the spread.
        limit = min(limit, bb)
        if not (0 < limit < ba - 1e-12):
            raise RuntimeError(f"{r['market_id']}: no passive price")
        legs.append({"row": r, "token": token, "price": limit, "touch": touch, "tick": tick, "min_order": min_order})
        capital_per_unit += r["weight_f"] * limit
    if capital_per_unit <= 0:
        raise RuntimeError("invalid bundle capital")
    cap = min(max_bundle_usd, rows[0]["max_notional_f"])
    units = cap / capital_per_unit
    for leg in legs:
        w, px = leg["row"]["weight_f"], leg["price"]
        if w * px > 0:
            units = min(units, max_leg_usd / (w * px))
        if w > 0:
            units = min(units, queue_fraction * max(0.0, leg["touch"]) / w)
    for leg in legs:
        shares = math.floor(units * leg["row"]["weight_f"] * 100.0) / 100.0
        if shares + 1e-12 < leg["min_order"]:
            raise RuntimeError(f"{leg['row']['market_id']}: tiny cap below min order size")
        leg["shares"] = shares
        leg["notional"] = shares * leg["price"]
    total = sum(x["notional"] for x in legs)
    if total <= 0 or total > max_bundle_usd + 1e-9 or any(x["notional"] > max_leg_usd + 1e-9 for x in legs):
        raise RuntimeError("pilot cap violation")
    return legs, total


def response_order_id(resp):
    if not isinstance(resp, dict):
        return None
    for k in ("orderID", "orderId", "id", "order_id"):
        if resp.get(k):
            return str(resp[k])
    return None


def matched_size(order):
    if not isinstance(order, dict):
        return 0.0
    for k in ("size_matched", "sizeMatched", "matched_size"):
        try:
            return max(0.0, float(order.get(k) or 0.0))
        except (TypeError, ValueError):
            pass
    return 0.0


def authenticated_client(host: str, chain_id: int):
    try:
        from py_clob_client_v2 import ApiCreds, ClobClient
    except ImportError as e:
        raise RuntimeError("pip install py_clob_client_v2 before --execute") from e
    missing = [k for k in REQUIRED_CREDS if not os.environ.get(k)]
    if missing:
        raise RuntimeError("missing credential environment variables: " + ",".join(missing))
    creds = ApiCreds(api_key=os.environ["CLOB_API_KEY"], api_secret=os.environ["CLOB_SECRET"], api_passphrase=os.environ["CLOB_PASS_PHRASE"])
    return ClobClient(host=host, chain_id=chain_id, key=os.environ["PK"], creds=creds)


def cancel_ids(client, ids):
    ids = [x for x in ids if x]
    if not ids:
        return
    try:
        client.cancel_orders(ids)
    except Exception as e:
        print(f"WARNING cancel_orders failed: {e}", file=sys.stderr)
        for oid in ids:
            try:
                from py_clob_client_v2 import OrderPayload
                client.cancel_order(OrderPayload(orderID=oid))
            except Exception as e2:
                print(f"WARNING cancel {oid} failed: {e2}", file=sys.stderr)


def unwind_fok(client, legs, matched, log):
    from py_clob_client_v2 import MarketOrderArgs, OrderType, PartialCreateOrderOptions, Side
    unresolved = []
    for leg, shares in zip(legs, matched):
        shares = math.floor(max(0.0, shares) * 100.0) / 100.0
        if shares <= 0:
            continue
        try:
            resp = client.create_and_post_market_order(
                order_args=MarketOrderArgs(token_id=leg["token"], amount=shares, side=Side.SELL, order_type=OrderType.FOK),
                options=PartialCreateOrderOptions(tick_size=str(leg["tick"])), order_type=OrderType.FOK)
            log.append({"action": "UNWIND_SELL_FOK", "token": leg["token"], "shares": shares, "response": resp})
        except Exception as e:
            unresolved.append({"token": leg["token"], "shares": shares, "error": str(e)})
    return unresolved


def execute_bundle(client, legs, args, log):
    from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side
    order_ids = []
    matched = [0.0] * len(legs)
    try:
        for leg in legs:
            resp = client.create_and_post_order(
                order_args=OrderArgs(token_id=leg["token"], price=leg["price"], side=Side.BUY, size=leg["shares"]),
                options=PartialCreateOrderOptions(tick_size=str(leg["tick"])),
                order_type=OrderType.GTC, post_only=True)
            oid = response_order_id(resp)
            if not oid:
                raise RuntimeError(f"order response has no id: {resp}")
            order_ids.append(oid)
            log.append({"action": "POST", "order_id": oid, "token": leg["token"], "price": leg["price"], "shares": leg["shares"], "response": resp})
    except Exception:
        cancel_ids(client, order_ids)
        raise

    deadline = time.time() + args.execution_seconds
    complete = False
    while time.time() < deadline:
        for i, oid in enumerate(order_ids):
            try:
                matched[i] = min(legs[i]["shares"], matched_size(client.get_order(oid)))
            except Exception as e:
                log.append({"action": "POLL_ERROR", "order_id": oid, "error": str(e)})
        fractions = [m / l["shares"] for m, l in zip(matched, legs)]
        if fractions and min(fractions) >= args.completion_threshold:
            complete = True
            break
        # Hard live leg-risk proxy: marked unmatched notional. Tiny by construction.
        exposed = sum(m * l["price"] for m, l in zip(matched, legs))
        if exposed > args.max_live_leg_risk_usd:
            break
        time.sleep(args.poll_seconds)

    cancel_ids(client, order_ids)
    time.sleep(max(0.0, args.cancel_grace_seconds))
    # Final matched snapshot after cancellation.
    for i, oid in enumerate(order_ids):
        try:
            matched[i] = min(legs[i]["shares"], matched_size(client.get_order(oid)))
        except Exception:
            pass
    log.append({"action": "ENTRY_FINAL", "complete": complete, "matched": matched})

    if complete:
        time.sleep(max(0.0, args.hold_seconds))
    unresolved = unwind_fok(client, legs, matched, log)
    return complete, matched, unresolved


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", type=Path, default=Path("runs/paper_v4/walk_forward.json"))
    ap.add_argument("--intents", type=Path, default=Path("runs/paper_v4/intents.csv"))
    ap.add_argument("--log", type=Path, default=Path("runs/tiny_live/pilot_log.jsonl"))
    ap.add_argument("--gamma-url", default="https://gamma-api.polymarket.com")
    ap.add_argument("--clob-url", default="https://clob.polymarket.com")
    ap.add_argument("--chain-id", type=int, default=137)
    ap.add_argument("--max-bundle-usd", type=float, default=10.0)
    ap.add_argument("--max-leg-usd", type=float, default=5.0)
    ap.add_argument("--queue-fraction", type=float, default=0.10)
    ap.add_argument("--min-edge", type=float, default=0.001)
    ap.add_argument("--execution-seconds", type=int, default=45)
    ap.add_argument("--poll-seconds", type=float, default=2.0)
    ap.add_argument("--completion-threshold", type=float, default=0.95)
    ap.add_argument("--max-live-leg-risk-usd", type=float, default=5.0)
    ap.add_argument("--cancel-grace-seconds", type=float, default=2.0)
    ap.add_argument("--hold-seconds", type=int, default=60)
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    if args.max_bundle_usd <= 0 or args.max_bundle_usd > 10.0 or args.max_leg_usd <= 0 or args.max_leg_usd > 5.0:
        raise SystemExit("tiny pilot hard caps: max bundle <= $10, max leg <= $5")
    report = load_report(args.report)
    if not report.get("eligible_for_tiny_pilot"):
        raise SystemExit("OOS report did not pass tiny-pilot gate: " + repr(report.get("gate_failures")))
    prod = report.get("production_threshold")
    if prod is None:
        raise SystemExit("OOS report has no production threshold")
    threshold = max(float(prod), args.min_edge)
    rows = choose_bundle(load_bundles(args.intents, int(time.time())), threshold)
    if not rows:
        raise SystemExit(f"no current complete bundle passes production threshold {threshold:.6f}")
    legs, total = preflight(rows, args.gamma_url, args.clob_url, args.max_bundle_usd, args.max_leg_usd, args.queue_fraction)
    plan = {"bundle_id": rows[0]["bundle_id"], "strategy": rows[0]["strategy"], "edge": rows[0]["expected_edge_f"],
            "production_threshold": threshold, "total_notional": total,
            "legs": [{k: x[k] for k in ("token", "price", "shares", "notional", "tick")} for x in legs]}
    print(json.dumps({"mode": "EXECUTE" if args.execute else "DRY_RUN", **plan}, indent=2, sort_keys=True))
    if not args.execute:
        return 0
    if os.environ.get("POLYMARKET_LIVE_ENABLE") != CONSENT:
        raise SystemExit(f"real submission blocked: set POLYMARKET_LIVE_ENABLE={CONSENT}")

    client = authenticated_client(args.clob_url, args.chain_id)
    log = [{"timestamp": int(time.time()), "action": "PILOT_START", "plan": plan}]
    try:
        complete, matched, unresolved = execute_bundle(client, legs, args, log)
        log.append({"timestamp": int(time.time()), "action": "PILOT_END", "complete": complete, "matched": matched, "unresolved": unresolved})
    finally:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        with args.log.open("a", encoding="utf-8") as f:
            for row in log:
                f.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    if unresolved:
        print("CRITICAL: unresolved tiny-pilot exposure remains; inspect pilot_log.jsonl immediately", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
