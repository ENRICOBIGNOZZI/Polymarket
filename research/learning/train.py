"""Nested chronological horse race with a final audit excluded from selection."""
from __future__ import annotations

from collections import Counter
import platform
import numpy as np
import scipy
import sklearn
from threadpoolctl import threadpool_limits

from .common import SAFETY, CONTEXTS, canonical, digest, publish
from .models import Candidate, FAMILIES, calibrate, calibrated
from .validation import DAY, expanding_folds, partition, metrics, paired_block_uncertainty, drift
from .execution import compare_policies

DEFAULT_POLICY = {"schema": "v7_learning_policy_v1", **SAFETY, "minimum_markets": 100,
    "minimum_time_blocks": 14, "embargo_ns": 300_000_000_000, "audit_fraction": .2,
    "ridges": [2., 8.], "weightings": ["market", "market_context", "signal", "decayed", "recent"],
    "calibrations": ["raw", "platt", "isotonic", "temperature"], "seed": 20260920,
    "minimum_net_edge": .02, "calibration_tolerance": .005,
    "tte_baseline_seconds": [105, 120], "duration_seconds": 7200}


def split_calibration(rows, stop, embargo):
    times = sorted({r["decision_ns"] for r in rows})
    if len(times) < 10:
        raise ValueError("INSUFFICIENT_CALIBRATION_HISTORY")
    boundary = times[int(.75*len(times))]
    (train, calibration), excluded = partition(rows, [times[0], boundary, stop], embargo_ns=embargo)
    if min(len({r["market_id"] for r in x}) for x in (train, calibration)) < 4:
        raise ValueError("INSUFFICIENT_CALIBRATION_MARKETS")
    return train, calibration, boundary


def inner_selection(family, rows, stop, policy):
    folds = expanding_folds(rows, end_ns=stop, folds=2, embargo_ns=policy["embargo_ns"])
    scores = []; failures = Counter()
    ridges = [0.] if family == "pm" else policy["ridges"]
    weightings = ["market"] if family == "pm" else policy["weightings"]
    for ridge in ridges:
        for weighting in weightings:
            by_method = {m: [] for m in policy["calibrations"] if family != "pm" or m == "raw"}
            for fold in folds:
                try:
                    train, cal, cut = split_calibration(fold["train"], fold["boundaries"][1], policy["embargo_ns"])
                    model = Candidate(family, ridge, weighting, policy["seed"]).fit(train, cut)
                    cp = model.predict(cal); vp = model.predict(fold["validation"])
                    for method in by_method:
                        try:
                            params = calibrate(cp, cal, method)
                            by_method[method].append(metrics(fold["validation"], calibrated(vp, params))["log_loss"])
                        except ValueError as exc:
                            failures[str(exc)] += 1
                except (ValueError, RuntimeError, Warning) as exc:
                    failures[type(exc).__name__+":"+str(exc)] += 1
            for method, values in by_method.items():
                if len(values) == len(folds):
                    scores.append({"ridge": ridge, "weighting": weighting, "calibration": method,
                                   "inner_log_loss": float(np.mean(values)), "inner_folds": len(values)})
    if not scores:
        raise ValueError("NO_VALID_NESTED_CANDIDATE:"+str(dict(failures)))
    best = min(scores, key=lambda c: (c["inner_log_loss"], c["ridge"], c["weighting"], c["calibration"]))
    return best, {"tested": scores, "failures": dict(failures)}


def fit_with_calibration(family, rows, stop, params, policy):
    train, cal, cut = split_calibration(rows, stop, policy["embargo_ns"])
    model = Candidate(family, params["ridge"], params["weighting"], policy["seed"]).fit(train, cut)
    calibration = calibrate(model.predict(cal), cal, params["calibration"])
    return model, calibration, {"train_rows": len(train), "calibration_rows": len(cal),
                                "fit_cutoff_ns": cut, "calibration_end_ns": stop,
                                "train_market_sha256": digest(canonical(sorted({r["market_id"] for r in train}))),
                                "calibration_market_sha256": digest(canonical(sorted({r["market_id"] for r in cal})))}


def history_diagnostic(family, development, audit, audit_start, params, policy, expanding_score):
    """One fixed 30-day comparison after primary selection, never a selector."""
    start=audit_start-30*DAY
    starts={}
    for row in development: starts[row['market_id']]=min(starts.get(row['market_id'],row['decision_ns']),row['decision_ns'])
    recent=[r for r in development if starts[r['market_id']]>=start]
    result={'state':'INSUFFICIENT_OLDER_HISTORY','recent_days':30,'selection_impact':'NONE',
            'expanding_rows':len(development),'recent_rows':len(recent)}
    if len(recent)==len(development) or len({r['market_id'] for r in recent})<30:
        return result
    try:
        model,cal,_=fit_with_calibration(family,recent,audit_start,params,policy)
        score=metrics(audit,calibrated(model.predict(audit),cal))
        result.update(state='DIAGNOSTIC_ONLY',expanding=expanding_score,recent=score,
                      recent_log_loss_better=score['log_loss']<expanding_score['log_loss'])
    except (ValueError,RuntimeError,Warning) as exc:
        result.update(state='INSUFFICIENT_RECENT_SUPPORT',reason=str(exc))
    return result


def train_stratum(rows, manifest, output, *, policy=None):
    policy = dict(DEFAULT_POLICY if policy is None else policy)
    if any(policy.get(k) != v for k, v in SAFETY.items()):
        raise ValueError("UNSAFE_TRAINING_POLICY")
    report = {"schema": "v7_learning_report_v1", **SAFETY, "dataset_sha256": manifest["dataset_sha256"],
              "code_sha": manifest["code_sha"], "cutoff_ns": manifest["cutoff_ns"],
              "policy_sha256": digest(canonical(policy)), "state": "REJECTED", "reasons": [],
              "forecast_quality": {}, "economic_quality": {}, "models_failed": {},
              "runtime_artifact_sha256": None,
              "environment": {"python": platform.python_version(), "numpy": np.__version__,
                              "scipy": scipy.__version__, "sklearn": sklearn.__version__}}
    if len({r["stratum"] for r in rows}) > 1:
        raise ValueError("INCOMPATIBLE_FEATURE_STRATA")
    eligible = [r for r in rows if r["training_eligible"] and r.get("outcome") is not None]
    markets = len({r["market_id"] for r in eligible}); blocks = len({r["decision_ns"]//DAY for r in eligible})
    report["data"] = {"rows": len(rows), "training_eligible_rows": len(eligible), "markets": markets,
                      "time_blocks": blocks, "contexts": dict(Counter(r["asset"]+":"+r["horizon"] for r in eligible))}
    report["pm_population_diagnostic"] = {"scope": "DESCRIPTIVE_NOT_FORWARD_VALIDATION",
        **metrics(eligible, [r["pm_probability"] for r in eligible])}
    support_ok = markets >= policy["minimum_markets"] and blocks >= policy["minimum_time_blocks"]
    if not support_ok:
        report["reasons"].append("INSUFFICIENT_DATA")
    if markets < 30 or blocks < 4:
        report["reasons"] = ["INSUFFICIENT_DATA"]
        report["forecast_quality"] = {family: {"state": "NOT_FIT_INSUFFICIENT_CHRONOLOGICAL_SUPPORT"}
                                      for family in FAMILIES}
        report["report_sha256"] = publish(output, "reports", report)
        return report
    times = sorted({r["decision_ns"] for r in rows})
    audit_start = times[int((1-policy["audit_fraction"])*len(times))]
    (development, audit), excluded = partition(rows, [times[0], audit_start, manifest["cutoff_ns"]],
                                               embargo_ns=policy["embargo_ns"])
    report["split"] = {"audit_start_ns": audit_start, "cutoff_ns": manifest["cutoff_ns"],
                       "exclusions": excluded, "development_rows": len(development), "audit_rows": len(audit)}
    if len({r["market_id"] for r in audit}) < 8:
        report["reasons"] = ["INSUFFICIENT_DATA"]
        report["report_sha256"] = publish(output, "reports", report)
        return report
    folds = expanding_folds(development, end_ns=audit_start, folds=3, embargo_ns=policy["embargo_ns"])
    report["outer_folds"] = [{k: v for k, v in f.items() if k not in {"train", "validation"}} for f in folds]
    comparisons = {}
    with threadpool_limits(limits=1):
        for family in FAMILIES:
            oos_rows = []; oos_p = []; selections = []
            try:
                for fold in folds:
                    params, search = inner_selection(family, fold["train"], fold["boundaries"][1], policy)
                    model, cal, fitting = fit_with_calibration(family, fold["train"], fold["boundaries"][1], params, policy)
                    p = calibrated(model.predict(fold["validation"]), cal)
                    oos_rows.extend(fold["validation"]); oos_p.extend(p.tolist())
                    selections.append({"parameters": params, "search": search, "fit": fitting})
                comparisons[family] = metrics(oos_rows, oos_p)
                report["forecast_quality"][family] = {"oos": comparisons[family], "folds": selections,
                    "paired_market_baseline": paired_block_uncertainty(oos_rows, oos_p, [r["pm_probability"] for r in oos_rows])}
            except (ValueError, RuntimeError, Warning) as exc:
                report["models_failed"][family] = str(exc)
        if not comparisons or "pm" not in comparisons:
            report["reasons"] = ["INSUFFICIENT_DATA"]
        else:
            winner = min(comparisons, key=lambda f: (comparisons[f]["log_loss"], f))
            # Selection freezes before audit evaluation. Audit never feeds fit/calibration.
            params, search = inner_selection(winner, development, audit_start, policy)
            model, cal, fitting = fit_with_calibration(winner, development, audit_start, params, policy)
            predicted = calibrated(model.predict(audit), cal)
            score = metrics(audit, predicted); baseline = metrics(audit, [r["pm_probability"] for r in audit])
            report.update(selected_forecaster=winner, audit=score, audit_pm=baseline,
                          final_fit=fitting, final_inner_search=search,
                          uncertainty=paired_block_uncertainty(audit, predicted, [r["pm_probability"] for r in audit]))
            report['history_diagnostic']=history_diagnostic(winner,development,audit,audit_start,params,policy,score)
            report["stability"] = {}
            for key in ("asset", "horizon"):
                report["stability"][key] = {value: metrics([r for r in audit if r[key] == value],
                    [p for r, p in zip(audit, predicted) if r[key] == value]) for value in sorted({r[key] for r in audit})}
            report["stability"]["tte_buckets"] = {}
            for low, high in ((0,30), (30,60), (60,105), (105,120), (120,180), (180,300)):
                matched = [(r, p) for r, p in zip(audit, predicted)
                           if r["features"].get("tte_seconds") is not None
                           and low <= r["features"]["tte_seconds"] < high]
                report["stability"]["tte_buckets"][f"{low}-{high}"] = metrics(
                    [r for r, _ in matched], [p for _, p in matched])
            report["drift"] = drift(development, audit, model.features.names)
            etrain, ecal, ecut = split_calibration(development, audit_start, policy["embargo_ns"])
            report["economic_quality"] = compare_policies(etrain, ecal, audit, predicted,
                train_cutoff=ecut, audit_cutoff=audit_start, threshold=policy["minimum_net_edge"])
            reasons = [] if support_ok else ["INSUFFICIENT_DATA"]
            if score["log_loss"] >= baseline["log_loss"] or score["brier"] >= baseline["brier"]:
                reasons.append("NO_INCREMENTAL_INFORMATION")
            if score["ece"] > baseline["ece"]+policy["calibration_tolerance"]:
                reasons.append("CALIBRATION_WORSE")
            if set(report["data"]["contexts"]) != set(CONTEXTS):
                reasons.append("UNSTABLE_CONTEXTS")
            economic = report["economic_quality"]
            net = economic["policies"]["uncertainty_adjusted"].get("net_pnl")
            if (net is None or net <= 0 or not economic["portfolio_capital_replay_verified"]
                    or not economic["counterfactual_transportability_validated"]):
                reasons.append("NO_EXECUTABLE_EDGE")
            report["reasons"] = reasons
            candidate = {"schema": "v7_offline_learning_candidate_v1", **SAFETY,
                         "dataset_sha256": manifest["dataset_sha256"], "code_sha": manifest["code_sha"],
                         "cutoff_ns": manifest["cutoff_ns"], "parameters": model.parameters(),
                         "calibration": cal, "policy_sha256": report["policy_sha256"],
                         "state": "TRAINED", "runtime_compatible": False,
                         "promotion_requires": "EXACT_NATIVE_FEATURE_AND_JOINT_EXECUTION_VALIDATION"}
            report["candidate_sha256"] = publish(output, "candidates", candidate)
            if all(r["stratum"] == "native_causal_features_v1" for r in development):
                from .artifact import export_native
                try:
                    ntrain, ncal, ncut = split_calibration(development, audit_start, policy["embargo_ns"])
                    sha, native = export_native(ntrain, ncal, dataset=manifest, output=output,
                                                fit_cutoff_ns=ncut, calibration_end_ns=audit_start)
                    report["native_candidate_sha256"] = sha
                    report["native_candidate_state"] = "TRAINED_REQUIRES_NATIVE_PARITY_AND_SEPARATE_OOS_VALIDATION"
                except ValueError as exc:
                    report["native_candidate_blocker"] = str(exc)
    report["report_sha256"] = publish(output, "reports", report)
    return report
