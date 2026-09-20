"""Two causal population views rebuilt exclusively from verified revisions."""
from __future__ import annotations

from collections import Counter, defaultdict
import math
from pathlib import Path
import sqlite3
import tempfile
import json

from .common import ASSETS, HORIZONS, SAFETY, canonical, digest, immutable, publish, source_rows
from .catalog import NATIVE, FAIR, LABEL
from v7_evidence_store import EvidenceStore
from v7_external_rich_train import build_rows as rich_rows
from v7_external_rich_model import features as rich_features

SCHEMA = "v7_cumulative_learning_dataset_v1"
FEATURE_CONTRACT = "native_causal_features_v1"
LABEL_CONTRACT = "binary_public_settlement_strict_information_time_v1"
NATIVE_FEATURES = (
    "pm_probability", "market_logit", "bid", "ask", "yes_bid", "yes_ask", "no_bid", "no_ask",
    "spread", "spread_ticks", "bid_depth", "ask_depth", "imbalance", "microprice",
    "tte_seconds", "log_tte", "signal_age_ms", "ttl_fraction", "book_age_ms", "book_pretrigger",
    "trigger_return_bp", "confirmation_return_bp", "venue_divergence_bp", "normalized_shock",
    "return_250ms", "return_1s", "return_5s", "event_vol_fast", "event_vol_slow",
    "hour_sin", "hour_cos", "weekday", "window_distance_seconds",
    "bid_depth_5", "ask_depth_5", "depth_5_imbalance", "market_age_seconds",
    "return_25ms", "return_50ms", "return_100ms", "return_500ms",
    "pm_return_100ms", "pm_volatility", "pm_update_intensity", "pm_ofi",
)


def finite(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def positive_int(v):
    return type(v) is int and v > 0


def native_example(r):
    """Clock comparisons stay within one capture. Wall mapping uses decision offset."""
    required = ("server_id", "run_id", "capture_id", "market_id", "token_id")
    code_sha = r.get("code_sha") or r.get("model_sha")
    if not isinstance(code_sha, str) or not code_sha:
        raise ValueError("MISSING_CODE_IDENTITY")
    if any(not isinstance(r.get(k), str) or not r[k] for k in required):
        raise ValueError("MISSING_CAUSAL_IDENTITY")
    if r.get("asset") not in ASSETS or r.get("horizon") not in HORIZONS:
        raise ValueError("UNKNOWN_CONTEXT")
    if r.get("paper_only") is not True or r.get("execution_authority") is not False:
        raise ValueError("INCOMPATIBLE_AUTHORITY")
    clocks = [r.get(k) for k in ("decision_monotonic_ns", "decision_wall_ns", "receive_monotonic_ns",
                                "trigger_monotonic_ns", "close_wall_ns", "close_monotonic_ns")]
    if not all(positive_int(t) for t in clocks):
        raise ValueError("MISSING_CAUSAL_CLOCK")
    decision, wall, book, trigger, close_wall, close = clocks
    if book > decision or trigger > decision or close <= decision:
        raise ValueError("FUTURE_FEATURE_OR_CLOSED_MARKET")
    grid = r.get("evaluated_grid_monotonic_ns")
    if not positive_int(grid) or grid > decision:
        raise ValueError("SIGNAL_FEATURE_INFORMATION_TIME_UNPROVEN")
    if abs((close_wall - close) - (wall - decision)) > 1_000_000:
        raise ValueError("INCONSISTENT_WALL_CLOCK_MAPPING")
    ext = r.get("external_features") or {}
    ext_time = ext.get("max_input_receive_ns", ext.get("input_receive_ns", ext.get("input_receive_monotonic_ns")))
    # Optional external diagnostics without their own information time are absent.
    ext_valid = positive_int(ext_time) and ext_time <= decision
    if positive_int(ext_time) and ext_time > decision:
        raise ValueError("FUTURE_EXTERNAL_FEATURE")
    if not positive_int(r.get("signal_version")) or not positive_int(r.get("sequence")):
        raise ValueError("MISSING_SIGNAL_OR_DECISION_ID")
    direction = r.get("direction")
    if direction not in (-1, 1):
        raise ValueError("MALFORMED_DIRECTION")
    if r.get("probability_forecast") is not None and r.get("probability_input_token_id") != r["token_id"]:
        raise ValueError("MODEL_INPUT_SIDE_MISMATCH_REQUIRES_EXPLICIT_ADAPTER")
    bid, ask = finite(r.get("bid_e4")), finite(r.get("ask_e4"))
    bq, aq, tick = finite(r.get("bid_quantity")), finite(r.get("ask_quantity")), finite(r.get("tick_e4"))
    valid_book = r.get("book_valid") is True and bid is not None and ask is not None and 0 < bid <= ask < 10000
    p = (bid + ask) / 20000 if valid_book else None
    total = bq + aq if bq is not None and aq is not None and bq >= 0 and aq > 0 else None
    shock = finite(r.get("binance_return_100ms_bp"))
    confirm = finite(r.get("confirmation_return_100ms_bp")) if r.get("confirmation_venue") in {"COINBASE", "BYBIT"} else None
    tte = (close - decision) / 1e9; age = (decision - trigger) / 1e6
    ttl = r.get("valid_until_monotonic_ns")
    ttl = (ttl - trigger) / 1e6 if positive_int(ttl) and ttl > trigger else None
    # Event-time RMS is deliberately not presented as short-horizon volatility.
    vol = finite(ext.get("volatility_100ms")) if ext_valid else None
    hour = (wall / 1e9 % 86400) / 3600
    features = dict.fromkeys(NATIVE_FEATURES)
    features.update(pm_probability=p, market_logit=math.log(p / (1 - p)) if p else None,
                    bid=bid / 10000 if valid_book else None, ask=ask / 10000 if valid_book else None,
                    spread=(ask - bid) / 10000 if valid_book else None,
                    spread_ticks=(ask - bid) / tick if valid_book and tick and tick > 0 else None,
                    bid_depth=bq / 1e6 if bq is not None and bq >= 0 else None,
                    ask_depth=aq / 1e6 if aq is not None and aq > 0 else None,
                    imbalance=(bq - aq) / total if total else None,
                    microprice=(ask*bq + bid*aq) / total / 10000 if valid_book and total else None,
                    tte_seconds=tte, log_tte=math.log(tte), signal_age_ms=age,
                    ttl_fraction=age / ttl if ttl else None, book_age_ms=(decision-book)/1e6,
                    book_pretrigger=float(book <= trigger), trigger_return_bp=shock,
                    confirmation_return_bp=confirm,
                    venue_divergence_bp=shock-confirm if shock is not None and confirm is not None else None,
                    normalized_shock=shock/(vol*10000) if shock is not None and vol and vol > 0 else None,
                    hour_sin=math.sin(hour*math.pi/12), hour_cos=math.cos(hour*math.pi/12),
                    weekday=float((wall//86_400_000_000_000 + 3) % 7),
                    window_distance_seconds=max(105-tte, tte-120, 0))
    for key in ("yes_bid", "yes_ask", "no_bid", "no_ask"):
        v = finite(r.get(key + "_e4"))
        if r.get("repricing_pair_valid") is True and v is not None and 0 < v < 10000:
            features[key] = v / 10000
    for key in ("return_250ms", "return_1s", "return_5s"):
        features[key] = finite(ext.get(key)) if ext_valid else None
    for key in ("fast", "slow"):
        features["event_vol_" + key] = finite(ext.get("native_vol_" + key)) if ext_valid else None
    for key in ("return_25ms", "return_50ms", "return_100ms", "return_500ms"):
        features[key] = finite(ext.get(key)) if ext_valid else None
    depths = []
    for side in ("bids", "asks"):
        levels = r.get(side)
        valid = isinstance(levels, list) and levels and all(isinstance(x, list) and len(x) == 2
            and finite(x[0]) is not None and 0 < x[0] < 10000 and finite(x[1]) is not None and x[1] >= 0 for x in levels)
        depths.append(sum(x[1] for x in levels[:5])/1e6 if valid else None)
    features["bid_depth_5"], features["ask_depth_5"] = depths
    if all(x is not None for x in depths) and sum(depths) > 0:
        features["depth_5_imbalance"] = (depths[0]-depths[1])/sum(depths)
    opened = r.get("market_open_wall_ns")
    if positive_int(opened) and opened <= wall:
        features["market_age_seconds"] = (wall-opened)/1e9
    signal = digest(canonical([r[k] for k in ("server_id", "run_id", "market_id")] + [r["signal_version"], trigger]))
    decision_id = digest(canonical([r[k] for k in ("server_id", "run_id", "capture_id")] + [r["sequence"]]))
    rate, exponent = finite(r.get("fee_rate")), finite(r.get("fee_exponent"))
    fee = None
    if (valid_book and rate is not None and exponent is not None and min(rate, exponent) >= 0
            and r.get("fee_source") and r.get("paper_terms_sha256")):
        fee = rate*((ask/10000)*(1-ask/10000))**exponent
    return {"decision_id": decision_id, "signal_id": signal, "market_id": r["market_id"],
            "token_id": r["token_id"], "asset": r["asset"], "horizon": r["horizon"],
            "code_sha": code_sha, "run_id": r["run_id"], "decision_ns": wall,
            "feature_information_ns": wall - decision + max(book, trigger, grid, ext_time if ext_valid else trigger),
            "information_end_ns": close_wall, "stratum": FEATURE_CONTRACT,
            "features": features, "native_input": {k: r.get(k) for k in (
                "direction", "bid_e4", "ask_e4", "bid_quantity", "ask_quantity", "signal_age_ns", "tte_ns",
                "binance_return_100ms_bp", "coinbase_return_100ms_bp", "confirmation_return_100ms_bp", "confirmation_venue")},
            "pm_probability": p, "accepted": r.get("accepted"), "reason": r.get("reason"),
            "fee_per_share_at_decision": fee,
            "outcome": None, "label_state": "PENDING", "label_information_ns": None,
            "execution": None, "execution_censored": True, "training_eligible": False}


def validate_label(label):
    if (label.get("schema") != LABEL or label.get("provider") != "POLYMARKET_GAMMA_PUBLIC"
            or label.get("closed") is not True or label.get("resolution_status") != "resolved"
            or not positive_int(label.get("information_ns"))):
        raise ValueError("UNVERIFIED_PUBLIC_SETTLEMENT")
    tokens = label.get("payouts")
    if not isinstance(tokens, dict) or len(tokens) != 2 or sorted(tokens.values()) != [0, 1]:
        raise ValueError("NONBINARY_SETTLEMENT")
    raw = label.get("public_response")
    if not isinstance(raw, dict) or digest(canonical(raw)) != label.get("public_response_sha256"):
        raise ValueError("SETTLEMENT_SOURCE_HASH_MISMATCH")
    # Verify facts against raw public response, not merely the adapter's flags.
    parse = lambda x: json.loads(x) if isinstance(x, str) else x
    ids, prices = parse(raw.get("clobTokenIds")), parse(raw.get("outcomePrices"))
    if (str(raw.get("id")) != str(label.get("market_id")) or raw.get("closed") is not True
            or raw.get("umaResolutionStatus") != "resolved" or not isinstance(ids, list)
            or not isinstance(prices, list) or len(ids) != 2 or len(prices) != 2
            or dict(zip(ids, map(float, prices))) != tokens):
        raise ValueError("SETTLEMENT_SOURCE_FACT_MISMATCH")
    return label


def pending_legacy(origin, cutoff_ns):
    """Keep proven causal opportunities even without a trustworthy final label."""
    cut = origin.get("rich_feature_cut")
    observed, start = finite(origin.get("observed_ms", origin.get("timestamp_ms"))), finite(origin.get("reference_version"))
    if (origin.get("evidence_semantics_version") != "external-fair-settlement-evidence-v2"
            or origin.get("paper_only") is not True or origin.get("authenticated_execution") is not False
            or origin.get("real_order_submission") is not False
            or origin.get("execution_authority") != "SHADOW_ZERO_AUTHORITY"
            or not origin.get("market_id") or not origin.get("forecast_id")
            or origin.get("market_mid_source") != "LIVE_COMPLEMENT_CONSISTENT_CLOB_BATCH"
            or not isinstance(cut, dict) or digest(canonical(cut)) != origin.get("rich_feature_sha256")
            or observed is None or start is None or not positive_int(cut.get("observed_wall_ns"))
            or not 0 < start*1e9 <= cut["observed_wall_ns"] <= observed*1e6 < min(cutoff_ns, (start+300)*1e9)):
        return None
    p = finite(cut.get("market_probability"))
    if p is None or not 0 < p < 1:
        return None
    try:
        features = rich_features(cut)
    except ValueError:
        return None
    key = digest(canonical([str(origin["market_id"]), str(origin["forecast_id"])]))
    return {"decision_id": key, "signal_id": key, "market_id": str(origin["market_id"]),
            "token_id": origin.get("yes_token"), "asset": "BTC", "horizon": "M5",
            "code_sha": origin.get("model_sha"), "decision_ns": int(observed*1e6),
            "feature_information_ns": cut["observed_wall_ns"], "information_end_ns": int((start+300)*1e9),
            "label_information_ns": None, "stratum": "legacy_rich_v1", "features": features,
            "pm_probability": p, "outcome": None, "label_state": "PENDING", "training_eligible": False,
            "accepted": None, "reason": "LEGACY_FORECAST_POPULATION", "execution": None,
            "execution_censored": True}


def materialize(store_root, revisions, output, cutoff_ns, code_sha, *, execution_scenario=None):
    exclusions = Counter(); decisions = {}; labels = {}; native = []; legacy_count = 0
    source_manifest = []; raw_rows = 0
    with tempfile.TemporaryDirectory(prefix="v7-learning-") as tmp, EvidenceStore(store_root) as store:
        db = sqlite3.connect(str(Path(tmp)/"records.sqlite"))
        db.execute("CREATE TABLE records(sha TEXT PRIMARY KEY, schema TEXT, identity TEXT, body TEXT, refs TEXT)")
        db.execute("CREATE INDEX record_family_identity ON records(schema,identity)")
        for revision in sorted(set(revisions)):
            rev = store.revision(revision)
            source_manifest.append({"revision": revision, "source_id": rev["source_id"], "contract": rev["contract"]})
            for row in source_rows(store, revision):
                if not isinstance(row, dict):
                    exclusions["NON_OBJECT_RECORD"] += 1; continue
                schema = row.get("schema")
                if schema not in {NATIVE, FAIR, LABEL}:
                    exclusions["SCHEMA_NOT_ADAPTED"] += 1; continue
                if schema == NATIVE and row.get("kind") != 2:
                    exclusions["NON_DECISION_NATIVE_EVENT_PRESERVED"] += 1; continue
                if schema == FAIR and row.get("event_type") not in {"FORECAST", "FORECAST_FINAL"}:
                    exclusions["NON_FORECAST_LEGACY_EVENT_PRESERVED"] += 1; continue
                payload = canonical(row); sha = digest(payload); raw_rows += 1
                prior = db.execute("SELECT refs FROM records WHERE sha=?", (sha,)).fetchone()
                if prior:
                    refs = sorted(set(json.loads(prior[0])) | {revision})
                    db.execute("UPDATE records SET refs=? WHERE sha=?", (json.dumps(refs), sha))
                    exclusions["DUPLICATE_SOURCE_RECORD"] += 1
                else:
                    db.execute("INSERT INTO records VALUES(?,?,?,?,?)", (sha, schema, row.get("forecast_id"), payload.decode(), json.dumps([revision])))
            db.commit()
        for sha, body, refs in db.execute("SELECT sha,body,refs FROM records WHERE schema=? ORDER BY sha", (LABEL,)):
            r = validate_label(json.loads(body)); key = str(r["market_id"])
            if r["information_ns"] > cutoff_ns:
                exclusions["LABEL_AFTER_CUTOFF"] += 1; continue
            if key in labels and labels[key]["payouts"] != r["payouts"]:
                raise ValueError("CONFLICTING_SETTLEMENT_LABELS")
            if key not in labels or r["information_ns"] < labels[key]["information_ns"]:
                labels[key] = {**r, "source_record_sha256": sha, "source_revisions": json.loads(refs)}
        for sha, body, refs in db.execute("SELECT sha,body,refs FROM records WHERE schema=? ORDER BY sha", (NATIVE,)):
            r = json.loads(body)
            try:
                row = native_example(r)
            except ValueError as exc:
                exclusions[str(exc)] += 1; continue
            if row["decision_ns"] >= cutoff_ns:
                exclusions["DECISION_AFTER_CUTOFF"] += 1; continue
            row.update(source_record_sha256=sha, source_revisions=json.loads(refs))
            label = labels.get(row["market_id"])
            if label:
                if not row["feature_information_ns"] <= row["decision_ns"] < label["information_ns"]:
                    raise ValueError("LEAKAGE_DETECTED")
                if row["token_id"] not in label["payouts"]:
                    exclusions["SETTLEMENT_TOKEN_MISMATCH"] += 1; continue
                row.update(outcome=label["payouts"][row["token_id"]], label_state="RESOLVED",
                           label_information_ns=label["information_ns"], label_source_sha256=label["source_record_sha256"],
                           label_source_revisions=label["source_revisions"])
                row["training_eligible"] = row["pm_probability"] is not None
            key = row["decision_id"]
            if key in decisions and decisions[key]["source_record_sha256"] != sha:
                raise ValueError("CONFLICTING_DECISION_IDENTITY")
            decisions[key] = row
        # Preserve older valid causal history as its own feature stratum. It is
        # never silently cast to the incompatible native runtime feature ABI.
        fids = db.execute("SELECT DISTINCT identity FROM records WHERE schema=? ORDER BY identity", (FAIR,)).fetchall()
        for (fid,) in fids:
            source = db.execute("SELECT sha,body,refs FROM records WHERE schema=? AND identity=? ORDER BY sha", (FAIR, fid)).fetchall()
            originals = [json.loads(v[1]) for v in source]
            rows, why = rich_rows(originals, cutoff_ns//1_000_000); exclusions.update(why)
            for r in rows:
                origin = next(v for v in originals if v.get("event_type") == "FORECAST")
                cut = origin.get("rich_feature_cut")
                if not isinstance(cut, dict) or not positive_int(cut.get("observed_wall_ns")):
                    exclusions["LEGACY_FEATURE_INFORMATION_TIME_UNPROVEN"] += 1; continue
                feature_ns = cut["observed_wall_ns"]; decision_ns = r["observed_ms"]*1_000_000
                if feature_ns > decision_ns:
                    exclusions["LEGACY_FEATURE_AFTER_DECISION"] += 1; continue
                key = digest(canonical([r["market_id"], r["forecast_id"]]))
                row = {"decision_id": key, "signal_id": key, "market_id": r["market_id"],
                       "token_id": origin.get("yes_token"),
                       "asset": "BTC", "horizon": "M5", "code_sha": r["source_code_sha"],
                       "decision_ns": decision_ns, "feature_information_ns": feature_ns,
                       "information_end_ns": (r["market_start_ms"]+300000)*1_000_000,
                       "label_information_ns": r["label_received_ms"]*1_000_000,
                       "stratum": "legacy_rich_v1", "features": r["features"],
                       "pm_probability": r["market_probability"], "outcome": r["actual"],
                       "label_state": "RESOLVED", "training_eligible": 0 < r["market_probability"] < 1,
                       "accepted": None, "reason": "LEGACY_FORECAST_POPULATION", "execution": None,
                       "execution_censored": True, "source_record_sha256": digest(canonical(originals)),
                       "source_revisions": sorted({x for v in source for x in json.loads(v[2])})}
                decisions[key] = row; legacy_count += 1
            for origin in originals:
                if origin.get("event_type") != "FORECAST":
                    continue
                pending = pending_legacy(origin, cutoff_ns)
                if pending is not None and pending["decision_id"] not in decisions:
                    pending.update(source_record_sha256=digest(canonical(origin)),
                        source_revisions=sorted({x for v in source for x in json.loads(v[2])}))
                    label = labels.get(pending["market_id"])
                    if label and pending["token_id"] in label["payouts"]:
                        if not pending["feature_information_ns"] <= pending["decision_ns"] < label["information_ns"]:
                            raise ValueError("LEAKAGE_DETECTED")
                        pending.update(outcome=label["payouts"][pending["token_id"]], label_state="RESOLVED",
                            label_information_ns=label["information_ns"], training_eligible=True,
                            label_source_sha256=label["source_record_sha256"],
                            label_source_revisions=label["source_revisions"])
                    decisions[pending["decision_id"]] = pending
                    legacy_count += 1
        db.close()
    action_rows = sorted(decisions.values(), key=lambda r: (r["decision_ns"], r["decision_id"]))
    signals = {}
    for row in action_rows:
        signals.setdefault(row["signal_id"], row)
    signal_rows = list(signals.values())
    if execution_scenario is not None:
        from .replay import replay_actions
        executions = replay_actions(store_root, revisions, signal_rows, **execution_scenario)
        for row in signal_rows:
            execution = executions[row["decision_id"]]
            if execution.get("information_ns", cutoff_ns+1) <= cutoff_ns:
                row["execution"] = execution
                row["execution_censored"] = execution.get("net_pnl") is None
    missing = Counter(k for row in signal_rows for k, v in row["features"].items() if v is None)
    manifest = {"schema": SCHEMA, **SAFETY, "code_sha": code_sha, "cutoff_ns": cutoff_ns,
                "feature_schema_sha256": digest(canonical([FEATURE_CONTRACT, NATIVE_FEATURES])),
                "labeling_schema_sha256": digest(LABEL_CONTRACT.encode()),
                "execution_scenario": execution_scenario,
                "builder_sha256": digest(Path(__file__).read_bytes()),
                "source_manifest": source_manifest, "source_rows": raw_rows,
                "decision_rows": len(action_rows), "signal_rows": len(signal_rows),
                "unique_markets": len({r["market_id"] for r in signal_rows}),
                "labeled_markets": len({r["market_id"] for r in signal_rows if r["outcome"] is not None}),
                "unresolved": sum(r["outcome"] is None for r in signal_rows),
                "legacy_stratum_rows": legacy_count, "exclusions": dict(sorted(exclusions.items())),
                "context_counts": dict(Counter(r["asset"]+":"+r["horizon"] for r in signal_rows)),
                "missing_fields": dict(sorted(missing.items())),
                "first_decision_ns": min((r["decision_ns"] for r in signal_rows), default=None),
                "last_decision_ns": max((r["decision_ns"] for r in signal_rows), default=None),
                "age_days": dict(Counter(str((cutoff_ns-r["decision_ns"])//86_400_000_000_000) for r in signal_rows)),
                "split_semantics": "WHOLE_MARKET_EXPANDING_PURGED_LABEL_AVAILABILITY_WITH_EMBARGO; assigned by trainer",
                "views": {}}
    for name, rows in (("signals", signal_rows), ("decisions", action_rows)):
        payload = b"".join(canonical(r)+b"\n" for r in rows); sha = digest(payload)
        immutable(Path(output)/"datasets"/(sha+".jsonl"), payload)
        manifest["views"][name] = {"sha256": sha, "rows": len(rows)}
    training_info = [{k: v for k, v in r.items() if k not in {"source_revisions", "label_source_revisions"}}
                     for r in signal_rows if r["training_eligible"]]
    manifest["training_information_sha256"] = digest(canonical(training_info))
    manifest["dataset_sha256"] = publish(output, "dataset_manifests", manifest)
    return manifest, signal_rows
