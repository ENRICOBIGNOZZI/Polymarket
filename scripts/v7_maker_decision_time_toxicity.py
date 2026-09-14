#!/usr/bin/env python3
"""Zero-authority decision-time toxicity research for the PAPER Maker.

Decision timestamps are observed book-event times while an order is alive.
Features use only information available at or before each timestamp. Future
fills and markouts are labels only; realized fill time never chooses a row.
Observations whose counterfactual KEEP outcome is hidden by an earlier terminal
order event are censored rather than mislabeled as no-fill negatives.
"""
from __future__ import annotations
import argparse, bisect, json, math, pathlib, sys
from collections import defaultdict
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import v7_maker_fill_conditioned_toxicity as base

SCHEMA = "polymarket_v7_maker_decision_time_toxicity_v1"
MODEL_SCHEMA = "polymarket_v7_maker_decision_time_toxicity_model_v1"
TERMINAL_STATES = {"CANCELLED", "EXPIRED", "REJECTED", "NONFILL", "FILLED"}
FEATURES = (
    "micro_score_available", "micro_score", "micro_score_change_from_entry",
    "minimum_micro_score_100ms", "micro_score_deterioration_100ms",
    "imbalance", "imbalance_change_from_entry", "minimum_imbalance_100ms",
    "ofi", "minimum_ofi_100ms", "maximum_cancel_intensity_100ms",
    "maximum_trade_intensity_100ms", "maximum_sell_print_rate_100ms",
    "last_sell_age_ms", "spread_ticks", "distance_from_touch_ticks",
    "queue_ahead", "fill_probability", "order_age_ms",
    "history_observations_100ms",
)

def num(value: Any, default: float = math.nan) -> float:
    return base.num(value, default)

def timestamp(row: dict[str, Any]) -> int:
    return int(num(row.get("recorded_ts_ms") or row.get("receive_ts_ms"), 0.0))

def token_id(order: dict[str, Any]) -> str:
    direct = str(order.get("token_id") or "")
    if direct:
        return direct
    meta = base.metadata(order)
    env = meta.get("opportunity_envelope") if isinstance(meta.get("opportunity_envelope"), dict) else {}
    plan = env.get("execution_plan") if isinstance(env.get("execution_plan"), dict) else {}
    legs = plan.get("legs") if isinstance(plan.get("legs"), list) else []
    return str(legs[0].get("token_id") or "") if len(legs) == 1 and isinstance(legs[0], dict) else ""

def canonical_order(row: dict[str, Any], actions: set[str]) -> bool:
    if row.get("event_type") != "ORDER_SUBMITTED" or not row.get("order_id"):
        return False
    if row.get("paper_only") is not True or row.get("authenticated_execution") is not False:
        return False
    meta = base.metadata(row)
    semantics = str(meta.get("execution_semantics_version") or "")
    return (
        meta.get("component") in {None, "professional_maker"}
        and (not semantics or semantics == base.SEMANTICS)
        and str(row.get("side") or meta.get("execution_side") or "").upper() == "BUY"
        and base.placement(row) in actions
        and meta.get("paper_bootstrap_probe") is not True
        and timestamp(row) > 0
        and bool(str(row.get("model_sha") or ""))
    )

def valid_book(row: dict[str, Any]) -> bool:
    return (
        row.get("schema") == "polymarket_v7_causal_book_observation_v1"
        and row.get("paper_only") is True
        and row.get("authenticated_execution") is False
        and row.get("real_order_submission") is False
        and row.get("execution_authority") == "ZERO_AUTHORITY_RESEARCH_ONLY"
        and row.get("valid") is True and row.get("features_valid") is True
        and row.get("lineage_continuous") is True
        and int(num(row.get("receive_wall_ms"), 0)) > 0
        and all(str(row.get(key) or "") for key in ("model_sha", "market_id", "token_id"))
    )

def book_features(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("placement_features")
    return value if isinstance(value, dict) else {}

def feature_value(row: dict[str, Any], name: str) -> float:
    return num(book_features(row).get(name))

def index_books(rows: list[dict[str, Any]]):
    books = defaultdict(list)
    sells = defaultdict(list)
    for row in rows:
        if not valid_book(row):
            continue
        key = (str(row["model_sha"]), str(row["market_id"]), str(row["token_id"]))
        books[key].append(row)
        trade = row.get("public_trade") if isinstance(row.get("public_trade"), dict) else None
        if trade and str(trade.get("aggressor_side") or "").upper() == "SELL":
            skey = key + (
                str(row.get("observer_session_id") or ""),
                int(num(row.get("connection_epoch"), 0)),
            )
            sells[skey].append(int(num(row.get("receive_wall_ms"), 0)))
    for values in books.values():
        values.sort(key=lambda r: (int(num(r.get("receive_wall_ms"), 0)), int(num(r.get("observer_sequence"), 0))))
    for values in sells.values():
        values.sort()
    return dict(books), dict(sells)

def markout_map(rows: list[dict[str, Any]], horizon: str):
    found: dict[tuple[str, str, str], tuple[int, float]] = {}
    for row in rows:
        values = row.get("markouts") if isinstance(row.get("markouts"), dict) else {}
        value = num(values.get(horizon))
        key = (str(row.get("model_sha") or ""), str(row.get("order_id") or ""), str(row.get("fill_id") or ""))
        when = timestamp(row)
        if row.get("event_type") != "MARKOUT" or not all(key) or when <= 0 or not math.isfinite(value):
            continue
        if key not in found or when < found[key][0]:
            found[key] = (when, value)
    return {key: value for key, (_, value) in found.items()}

def first_fill_map(rows: list[dict[str, Any]]):
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if row.get("event_type") != "FILL" or num(row.get("filled_size"), 0) <= 0:
            continue
        key, when = base.life(row), timestamp(row)
        if not all(key) or when <= 0:
            continue
        if key not in found or when < timestamp(found[key]):
            found[key] = row
    return found

def terminal_map(rows: list[dict[str, Any]]):
    found: dict[tuple[str, str], int] = {}
    for row in rows:
        if row.get("event_type") != "ORDER_STATE" or str(row.get("order_state") or "").upper() not in TERMINAL_STATES:
            continue
        key, when = base.life(row), timestamp(row)
        if all(key) and when > 0:
            found[key] = min(found.get(key, when), when)
    return found

def latest_age(times: list[int], now_ms: int) -> float:
    pos = bisect.bisect_right(times, now_ms)
    return float(max(0, now_ms - times[pos - 1])) if pos else math.nan

def rolling(history: list[dict[str, Any]], current: dict[str, Any], window_ms: int):
    now = int(num(current.get("receive_wall_ms"), 0))
    session = str(current.get("observer_session_id") or "")
    epoch = int(num(current.get("connection_epoch"), 0))
    return [
        row for row in history
        if now - max(0, window_ms) <= int(num(row.get("receive_wall_ms"), 0)) <= now
        and str(row.get("observer_session_id") or "") == session
        and int(num(row.get("connection_epoch"), 0)) == epoch
    ]

def finite_values(rows: list[dict[str, Any]], name: str) -> list[float]:
    return [v for v in (feature_value(row, name) for row in rows) if math.isfinite(v)]

def decision_features(order, current, history, sell_times, *, trailing_ms: int):
    now = int(num(current.get("receive_wall_ms"), 0))
    entry = base.feature_cut(order)
    recent = rolling(history, current, trailing_ms)
    scores = finite_values(recent, "microstructure_shadow_delta_250ms")
    imbalances = finite_values(recent, "imbalance")
    ofis = finite_values(recent, "ofi")
    cancels = finite_values(recent, "cancel_intensity")
    trades = finite_values(recent, "trade_intensity")
    sell_rates = finite_values(recent, "aggressive_sell_prints_per_second")
    score = feature_value(current, "microstructure_shadow_delta_250ms")
    imbalance = feature_value(current, "imbalance")
    entry_score, entry_imbalance = num(entry.get("microstructure_shadow_delta_250ms")), num(entry.get("imbalance"))
    return {
        "micro_score_available": float(math.isfinite(score)),
        "micro_score": score,
        "micro_score_change_from_entry": score-entry_score if math.isfinite(score) and math.isfinite(entry_score) else math.nan,
        "minimum_micro_score_100ms": min(scores, default=math.nan),
        "micro_score_deterioration_100ms": max(scores)-score if scores and math.isfinite(score) else math.nan,
        "imbalance": imbalance,
        "imbalance_change_from_entry": imbalance-entry_imbalance if math.isfinite(imbalance) and math.isfinite(entry_imbalance) else math.nan,
        "minimum_imbalance_100ms": min(imbalances, default=math.nan),
        "ofi": feature_value(current, "ofi"),
        "minimum_ofi_100ms": min(ofis, default=math.nan),
        "maximum_cancel_intensity_100ms": max(cancels, default=math.nan),
        "maximum_trade_intensity_100ms": max(trades, default=math.nan),
        "maximum_sell_print_rate_100ms": max(sell_rates, default=math.nan),
        "last_sell_age_ms": latest_age(sell_times, now),
        "spread_ticks": feature_value(current, "spread_ticks"),
        "distance_from_touch_ticks": feature_value(current, "distance_from_touch_ticks"),
        "queue_ahead": num(entry.get("queue_ahead")),
        "fill_probability": num(entry.get("fill_probability")),
        "order_age_ms": float(max(0, now-timestamp(order))),
        "history_observations_100ms": float(len(recent)),
    }

def build_decision_rows(
    maker_rows, book_rows, *, markout_horizon: str, placement_actions: set[str],
    cancel_latency_ms: float, fill_hazard_window_ms: int,
    minimum_decision_spacing_ms: int, trailing_ms: int = 100,
):
    orders = {base.life(row): row for row in maker_rows if canonical_order(row, placement_actions)}
    fills, terminals = first_fill_map(maker_rows), terminal_map(maker_rows)
    markouts, (books, sells) = markout_map(maker_rows, markout_horizon), index_books(book_rows)
    out = []
    diag = {key: 0 for key in (
        "orders_missing_token", "orders_missing_book_path",
        "decision_rows_with_first_fill_in_horizon",
        "decision_rows_with_avoidable_adverse_fill",
        "decision_rows_with_unavoidable_fill",
        "decision_rows_missing_fill_markout", "decision_rows_right_censored",
        "decision_rows_policy_censored",
    )}
    diag["candidate_orders"] = len(orders)
    for key, order in orders.items():
        sha, order_id = key
        market, token, start = str(order.get("market_id") or ""), token_id(order), timestamp(order)
        if not token:
            diag["orders_missing_token"] += 1
            continue
        fill, terminal = fills.get(key), terminals.get(key, 0)
        fill_ms = timestamp(fill) if fill else 0
        ends = [value for value in (fill_ms, terminal) if value > start]
        life_end = min(ends) if ends else 0
        path = [
            row for row in books.get((sha, market, token), [])
            if int(num(row.get("receive_wall_ms"), 0)) >= start
            and (life_end <= 0 or int(num(row.get("receive_wall_ms"), 0)) < life_end)
        ]
        if not path:
            diag["orders_missing_book_path"] += 1
            continue
        fill_id = str(fill.get("fill_id") or "") if fill else ""
        fill_shares = max(0.0, num(fill.get("filled_size"), 0)) if fill else 0.0
        markout = markouts.get((sha, order_id, fill_id), math.nan) if fill_id else math.nan
        last_decision, history = -10**30, []
        for row in path:
            now = int(num(row.get("receive_wall_ms"), 0))
            history.append(row)
            if now-last_decision < max(0, minimum_decision_spacing_ms):
                continue
            last_decision = now
            effective = int(math.ceil(now+max(0.0, cancel_latency_ms)))
            horizon_end = effective+max(1, int(fill_hazard_window_ms))
            unavoidable = fill_ms > 0 and fill_ms <= effective
            in_window = fill_ms > effective and fill_ms <= horizon_end
            adverse = favorable = missing = False
            censor_reason = ""
            if unavoidable:
                diag["decision_rows_with_unavoidable_fill"] += 1
            elif in_window:
                diag["decision_rows_with_first_fill_in_horizon"] += 1
                if math.isfinite(markout):
                    adverse, favorable = markout < 0, markout > 0
                    diag["decision_rows_with_avoidable_adverse_fill"] += int(adverse)
                else:
                    missing = True
                    censor_reason = "MISSING_FILL_MARKOUT"
                    diag["decision_rows_missing_fill_markout"] += 1
            elif terminal > now and terminal < horizon_end:
                # The historical policy removed the order before the KEEP
                # counterfactual horizon ended. Treat it as censored, not as a
                # no-fill negative; otherwise the model learns the old policy.
                missing = True
                censor_reason = "HISTORICAL_TERMINAL_BEFORE_KEEP_HORIZON"
                diag["decision_rows_policy_censored"] += 1
            elif fill_ms <= 0 and terminal <= 0:
                # No terminal proof means log end/feed continuation cannot prove
                # that the live order survived the complete KEEP horizon.
                missing = True
                censor_reason = "ORDER_LIFETIME_RIGHT_CENSORED"
                diag["decision_rows_right_censored"] += 1
            session = str(row.get("observer_session_id") or "")
            epoch = int(num(row.get("connection_epoch"), 0))
            f = decision_features(order, row, history, sells.get((sha, market, token, session, epoch), []), trailing_ms=trailing_ms)
            out.append({
                "source_model_sha": sha, "order_id": order_id, "order_ts_ms": start,
                "market_id": market, "event_cluster": str(order.get("event_id") or market or "UNKNOWN"),
                "token_id": token, "decision_ts_ms": now,
                "decision_snapshot_id": str(row.get("observer_sequence") or ""),
                "observer_session_id": session, "connection_epoch": epoch,
                "cancel_latency_ms": float(cancel_latency_ms),
                "fill_hazard_window_ms": int(fill_hazard_window_ms),
                "effective_cancel_ms": effective, "first_fill_ts_ms": fill_ms or None,
                "terminal_ts_ms": terminal or None,
                "unavoidable_fill_before_cancel_effective": int(unavoidable),
                "first_fill_in_horizon": int(in_window),
                "avoidable_adverse_fill": int(adverse),
                "avoidable_favorable_fill": int(favorable),
                "label_complete": not missing, "censor_reason": censor_reason,
                "first_fill_id": fill_id, "first_fill_shares": fill_shares,
                "first_fill_markout_per_share": markout if math.isfinite(markout) else None,
                "features": f,
            })
    diag["decision_rows"] = len(out)
    return out, diag

def supervised_rows(rows):
    return [row for row in rows if row.get("label_complete") is True]

def fit_preprocessor(rows):
    result = {}
    for name in FEATURES:
        values = [num(row["features"].get(name)) for row in rows]
        values = [v for v in values if math.isfinite(v)]
        if not values:
            result[name] = {"lower":0.0,"upper":0.0,"median":0.0,"scale":1.0,"observed":0}
            continue
        lo, hi, med = base.quantile(values,.02), base.quantile(values,.98), base.quantile(values,.50)
        scale = base.quantile(values,.90)-base.quantile(values,.10)
        if not math.isfinite(scale) or abs(scale) < 1e-9:
            scale = max(abs(med),1.0)
        result[name] = {"lower":lo,"upper":hi,"median":med,"scale":scale,"observed":len(values)}
    return result

def transform(row, prep):
    out = [1.0]
    for name in FEATURES:
        spec, value = prep[name], num(row["features"].get(name), prep[name]["median"])
        if not math.isfinite(value):
            value = spec["median"]
        value = min(spec["upper"], max(spec["lower"], value))
        out.append((value-spec["median"])/spec["scale"])
    return out

def cluster_weights(rows):
    counts = defaultdict(int)
    for row in rows:
        counts[str(row["event_cluster"])] += 1
    weights = [1.0/counts[str(row["event_cluster"])] for row in rows]
    total = sum(weights)
    return [w*len(rows)/total for w in weights] if total else [1.0]*len(rows)

def sigmoid(value):
    return 1.0/(1.0+math.exp(-max(-35.0,min(35.0,value))))

def fit_logistic(rows, prep, *, target: str, ridge: float, iterations: int):
    beta = [0.0]*(len(FEATURES)+1)
    design, labels, weights = [transform(r,prep) for r in rows], [float(r[target]) for r in rows], cluster_weights(rows)
    denom = max(1.0,sum(weights))
    for iteration in range(max(1,iterations)):
        grad = [0.0]*len(beta)
        for x,label,weight in zip(design,labels,weights):
            error = (sigmoid(sum(b*v for b,v in zip(beta,x)))-label)*weight
            for i,value in enumerate(x):
                grad[i] += error*value
        for i in range(len(beta)):
            grad[i] /= denom
            if i:
                grad[i] += ridge*beta[i]/max(1.0,len(rows))
        rate = .20/math.sqrt(1.0+iteration/100.0)
        step = max(abs(v) for v in grad)
        beta = [b-rate*g for b,g in zip(beta,grad)]
        if step < 1e-7:
            break
    return beta

def predict(row, prep, beta):
    return sigmoid(sum(b*v for b,v in zip(beta,transform(row,prep))))

def auc(rows, prep, beta, target):
    return base.auc([int(r[target]) for r in rows],[predict(r,prep,beta) for r in rows])

def brier(rows, prep, beta, target):
    return None if not rows else sum((predict(r,prep,beta)-float(r[target]))**2 for r in rows)/len(rows)

def coefficient_map(beta):
    return {"intercept":beta[0], **{name:beta[i+1] for i,name in enumerate(FEATURES)}}

def risk_buckets(rows, prep, beta, target):
    scored = sorted(((predict(r,prep,beta),r) for r in rows), key=lambda item:item[0])
    if not scored:
        return []
    out, count = [], min(10,len(scored))
    for bucket in range(count):
        values = scored[bucket*len(scored)//count:(bucket+1)*len(scored)//count]
        if values:
            out.append({
                "bucket":bucket+1, "rows":len(values),
                "mean_predicted_probability":sum(score for score,_ in values)/len(values),
                "observed_rate":sum(int(row[target]) for _,row in values)/len(values),
                "clusters":len({str(row["event_cluster"]) for _,row in values}),
            })
    return out

def fit_one_latency(rows, *, minimum_clusters:int, ridge:float, iterations:int):
    rows = supervised_rows(rows)
    clusters = {str(row["event_cluster"]) for row in rows}
    if len(clusters) < minimum_clusters:
        return {"state":"INSUFFICIENT_DECISION_TIME_EVIDENCE","rows":len(rows),"clusters":len(clusters),"minimum_clusters":minimum_clusters}
    train,val,test,split = base.chronological_cluster_split(rows)
    prep = fit_preprocessor(train)
    bad = fit_logistic(train,prep,target="avoidable_adverse_fill",ridge=ridge,iterations=iterations)
    fill = fit_logistic(train,prep,target="first_fill_in_horizon",ridge=ridge,iterations=iterations)
    core = {
        "schema":MODEL_SCHEMA,"paper_only":True,"authenticated_execution":False,
        "real_order_submission":False,"execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY",
        "automatic_promotion":False,"feature_names":list(FEATURES),"preprocessing":prep,
        "avoidable_adverse_fill_coefficients":coefficient_map(bad),
        "first_fill_hazard_coefficients":coefficient_map(fill),"threshold":None,
        "threshold_role":"NONE_SCORE_REQUIRES_SEPARATE_FORWARD_SHADOW_POLICY_REPLAY",
        "cancel_latency_ms":rows[0]["cancel_latency_ms"],
        "fill_hazard_window_ms":rows[0]["fill_hazard_window_ms"],
        "split_cluster_sha256":{name:base.sha256(values) for name,values in split.items()},
    }
    return {
        "state":"FIT_COMPLETE_ZERO_AUTHORITY",
        "model":{**core,"model_hash":base.sha256(core)},
        "split":{name:{"clusters":len(values),"sha256":base.sha256(values)} for name,values in split.items()},
        "metrics":{
            "train_adverse_auc":auc(train,prep,bad,"avoidable_adverse_fill"),
            "validation_adverse_auc":auc(val,prep,bad,"avoidable_adverse_fill"),
            "test_adverse_auc":auc(test,prep,bad,"avoidable_adverse_fill"),
            "test_adverse_brier":brier(test,prep,bad,"avoidable_adverse_fill"),
            "train_fill_auc":auc(train,prep,fill,"first_fill_in_horizon"),
            "validation_fill_auc":auc(val,prep,fill,"first_fill_in_horizon"),
            "test_fill_auc":auc(test,prep,fill,"first_fill_in_horizon"),
            "test_fill_brier":brier(test,prep,fill,"first_fill_in_horizon"),
            "test_adverse_risk_buckets":risk_buckets(test,prep,bad,"avoidable_adverse_fill"),
        },
        "policy_gate":{"cancel_authority":False,"requires_event_driven_shadow_replay":True,"requires_forward_paper_replication":True},
    }

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--maker-evidence",type=pathlib.Path,action="append",required=True)
    p.add_argument("--book-evidence",type=pathlib.Path,action="append",required=True)
    p.add_argument("--output",type=pathlib.Path,required=True)
    p.add_argument("--markout-horizon",default="250ms")
    p.add_argument("--placement-action",action="append",default=[])
    p.add_argument("--cancel-latency-ms",default="5,25,50,100")
    p.add_argument("--fill-hazard-window-ms",type=int,default=500)
    p.add_argument("--minimum-decision-spacing-ms",type=int,default=25)
    p.add_argument("--trailing-feature-window-ms",type=int,default=100)
    p.add_argument("--minimum-clusters",type=int,default=20)
    p.add_argument("--ridge",type=float,default=2.0)
    p.add_argument("--iterations",type=int,default=2500)
    args = p.parse_args()
    actions = {str(v).upper() for v in args.placement_action} or {"JOIN","IMPROVE1"}
    maker,books = base.records(args.maker_evidence),base.records(args.book_evidence)
    latencies = []
    for raw in str(args.cancel_latency_ms).split(","):
        value = num(raw.strip())
        if not math.isfinite(value) or value < 0:
            raise SystemExit("invalid --cancel-latency-ms")
        latencies.append(float(value))
    if args.fill_hazard_window_ms <= 0 or args.minimum_decision_spacing_ms < 0:
        raise SystemExit("invalid decision-time horizon/spacing")
    results = []
    for latency in sorted(set(latencies)):
        rows,diag = build_decision_rows(
            maker,books,markout_horizon=str(args.markout_horizon),placement_actions=actions,
            cancel_latency_ms=latency,fill_hazard_window_ms=args.fill_hazard_window_ms,
            minimum_decision_spacing_ms=args.minimum_decision_spacing_ms,
            trailing_ms=args.trailing_feature_window_ms,
        )
        results.append({"cancel_latency_ms":latency,"diagnostics":diag,**fit_one_latency(
            rows,minimum_clusters=args.minimum_clusters,ridge=args.ridge,iterations=args.iterations)})
    report = {
        "schema":SCHEMA,"paper_only":True,"authenticated_execution":False,
        "real_order_submission":False,"execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY",
        "automatic_promotion":False,"decision_clock":"OBSERVED_BOOK_EVENT_TIMESTAMP_WHILE_ORDER_ALIVE",
        "feature_cut":"ONLY_DATA_AT_OR_BEFORE_DECISION_TIMESTAMP",
        "future_fill_time_role":"LABEL_ONLY_NEVER_USED_TO_CHOOSE_DECISION_TIMESTAMP",
        "historical_terminal_role":"CENSOR_IF_KEEP_COUNTERFACTUAL_HORIZON_NOT_OBSERVED",
        "target":"P(AVOIDABLE_ADVERSE_FIRST_FILL_WITHIN_FORWARD_WINDOW|CURRENT_CAUSAL_STATE)",
        "markout_horizon":str(args.markout_horizon),
        "fill_hazard_window_ms":args.fill_hazard_window_ms,
        "minimum_decision_spacing_ms":args.minimum_decision_spacing_ms,
        "trailing_feature_window_ms":args.trailing_feature_window_ms,
        "placement_actions":sorted(actions),"latency_models":results,
        "promotion_gate":"NONE_RESEARCH_ONLY_REQUIRES_EVENT_DRIVEN_SHADOW_REPLAY_AND_FORWARD_PAPER_REPLICATIONS",
    }
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    print(json.dumps(report,indent=2,sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
