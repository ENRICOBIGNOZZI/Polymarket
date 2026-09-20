"""Market-grouped chronological evaluation and dependence-aware diagnostics."""
from __future__ import annotations

from collections import Counter, defaultdict
import math
import numpy as np

DAY = 86_400_000_000_000


def weights(rows, mode="market", *, cutoff_ns=None, half_life_days=30):
    if mode not in {"market", "market_context", "signal", "decayed", "recent"}:
        raise ValueError("UNKNOWN_WEIGHTING")
    key = lambda r: (r["market_id"], r["asset"], r["horizon"]) if mode == "market_context" else r["market_id"]
    counts = Counter(key(r) for r in rows)
    signal_counts = Counter(r["signal_id"] for r in rows)
    signals_per_market = defaultdict(set)
    for r in rows:
        signals_per_market[r["market_id"]].add(r["signal_id"])
    values = []
    for r in rows:
        w = 1 / counts[key(r)]
        if mode == "signal":
            w = 1 / (len(signals_per_market[r["market_id"]]) * signal_counts[r["signal_id"]])
        if mode in {"decayed", "recent"}:
            if cutoff_ns is None or half_life_days <= 0:
                raise ValueError("WEIGHT_CUTOFF_REQUIRED")
            age = max(0, (cutoff_ns-r["decision_ns"])/DAY)
            w *= 2**(-age/half_life_days) if mode == "decayed" else float(age <= half_life_days)
        values.append(w)
    return np.asarray(values)


def partition(rows, boundaries, *, embargo_ns):
    """Purge whole markets that straddle any boundary, including pending rows.

    Training includes only outcomes observed before its partition ends minus
    embargo. Validation and audit label availability are checked the same way.
    Contract end purges overlapping event windows even when a label is early.
    """
    if embargo_ns < 0 or sorted(set(boundaries)) != list(boundaries) or len(boundaries) < 2:
        raise ValueError("INVALID_CHRONOLOGICAL_BOUNDARIES")
    groups = defaultdict(list); excluded = Counter(); seen = set()
    for r in rows:
        if r["decision_id"] in seen:
            raise ValueError("DUPLICATE_DECISION")
        seen.add(r["decision_id"])
        if r["feature_information_ns"] > r["decision_ns"]:
            raise ValueError("LEAKAGE_DETECTED")
        if r.get("outcome") is not None and (r["outcome"] not in (0, 1) or not r["decision_ns"] < r["label_information_ns"]):
            raise ValueError("INVALID_LABEL_TIME_OR_VALUE")
        groups[r["market_id"]].append(r)
    parts = [[] for _ in range(len(boundaries)-1)]
    for market, group in sorted(groups.items()):
        first = min(r["decision_ns"] for r in group)
        last = max(r["decision_ns"] for r in group)
        idx = next((i for i, (start, stop) in enumerate(zip(boundaries, boundaries[1:]))
                    if start <= first <= last < stop), None)
        if idx is None:
            excluded["MARKET_CROSSES_BOUNDARY"] += len(group); continue
        if idx > 0 and first < boundaries[idx]+embargo_ns:
            excluded["EMBARGO_AFTER_BOUNDARY"] += len(group); continue
        stop = boundaries[idx+1]
        # All rows of a market participate, including pending/invalid rows.
        if max(r["information_end_ns"] for r in group) + embargo_ns >= stop:
            excluded["PURGED_OVERLAPPING_EVENT_WINDOW"] += len(group); continue
        for r in group:
            if not r.get("training_eligible") or r.get("outcome") is None:
                excluded["PENDING_OR_FEATURE_INELIGIBLE"] += 1; continue
            if r["label_information_ns"] + embargo_ns >= stop:
                excluded["LABEL_NOT_AVAILABLE_BEFORE_BOUNDARY"] += 1; continue
            parts[idx].append(r)
    for p in parts:
        p.sort(key=lambda r: (r["decision_ns"], r["decision_id"]))
    return parts, dict(excluded)


def expanding_folds(rows, *, end_ns, folds=3, embargo_ns=300_000_000_000):
    times = sorted({r["decision_ns"] for r in rows if r["decision_ns"] < end_ns})
    if len(times) < (folds+2)*2:
        raise ValueError("INSUFFICIENT_TIME_BLOCKS")
    boundaries = [times[max(1, len(times)*i//(folds+2))] for i in range(2, folds+2)] + [end_ns]
    output = []
    for split, stop in zip(boundaries, boundaries[1:]):
        (train, valid), excluded = partition(rows, [times[0], split, stop], embargo_ns=embargo_ns)
        if train and valid:
            output.append({"train": train, "validation": valid, "boundaries": [times[0], split, stop], "excluded": excluded})
    if not output:
        raise ValueError("INSUFFICIENT_PURGED_FOLDS")
    return output


def logit(p):
    p = np.clip(p, 1e-8, 1-1e-8)
    return np.log(p/(1-p))


def sigmoid(z):
    return 1/(1+np.exp(-np.clip(z, -40, 40)))


def calibration_fit(p, y, w, *, temperature=False):
    x = logit(np.asarray(p))
    X = x[:, None] if temperature else np.column_stack([np.ones(len(x)), x])
    b = np.array([1.] if temperature else [0., 1.])
    if len(set(y)) < 2:
        raise ValueError("CALIBRATION_REQUIRES_BOTH_OUTCOMES")
    for _ in range(100):
        q = sigmoid(X@b)
        step = np.linalg.solve(X.T@((w*q*(1-q))[:, None]*X)+np.eye(X.shape[1])*1e-6,
                               X.T@(w*(q-y)))
        if np.linalg.norm(step) > 1:
            step /= np.linalg.norm(step)
        b -= step
        if np.linalg.norm(step) < 1e-8:
            break
    return {"intercept": 0. if temperature else float(b[0]), "slope": float(b[-1])}


def metrics(rows, p):
    if not rows:
        return {"rows": 0, "markets": 0, "log_loss": None, "brier": None, "ece": None}
    y = np.asarray([r["outcome"] for r in rows]); w = weights(rows); p = np.asarray(p)
    if len(p) != len(y) or not np.isfinite(p).all() or ((p < 0) | (p > 1)).any():
        raise ValueError("INVALID_PREDICTIONS")
    p = np.clip(p, 1e-8, 1-1e-8); reliability = []; ece = 0.
    for lo in np.arange(0., 1., .1):
        mask = (p >= lo) & (p < lo+.1)
        mass = float(w[mask].sum())
        pred = float(np.average(p[mask], weights=w[mask])) if mass else None
        observed = float(np.average(y[mask], weights=w[mask])) if mass else None
        if mass:
            ece += mass/w.sum()*abs(pred-observed)
        reliability.append({"lower": float(lo), "rows": int(mask.sum()), "market_weight": mass,
                            "forecast": pred, "observed": observed})
    try:
        calibration = calibration_fit(p, y, w)
    except ValueError:
        calibration = {"intercept": None, "slope": None}
    return {"rows": len(rows), "markets": len({r["market_id"] for r in rows}),
            "log_loss": float(np.average(-y*np.log(p)-(1-y)*np.log1p(-p), weights=w)),
            "brier": float(np.average((p-y)**2, weights=w)), "ece": float(ece),
            "calibration": calibration, "reliability": reliability}


def paired_block_uncertainty(rows, predictions, baseline, seed=20260920, replicates=256):
    """Resample settlement-day blocks; market weighting inside each block."""
    blocks = defaultdict(list)
    for i, r in enumerate(rows):
        blocks[r["information_end_ns"]//DAY].append(i)
    deltas = []
    for indices in blocks.values():
        rs = [rows[i] for i in indices]
        a = metrics(rs, np.asarray(predictions)[indices]); b = metrics(rs, np.asarray(baseline)[indices])
        deltas.append(a["log_loss"]-b["log_loss"])
    if len(deltas) < 4:
        return {"blocks": len(deltas), "interval": None, "reason": "INSUFFICIENT_TIME_BLOCKS"}
    rng = np.random.default_rng(seed)
    draws = np.mean(rng.choice(deltas, (replicates, len(deltas)), replace=True), axis=1)
    return {"blocks": len(deltas), "interval": np.quantile(draws, [.025, .975]).tolist(),
            "mean_logloss_delta": float(np.mean(deltas)),
            "semantics": "PAIRED_SETTLEMENT_DAY_BLOCK_BOOTSTRAP_NOT_CONDITIONAL_COVERAGE"}


def drift(train, recent, names):
    out = {}
    for key in names:
        a = np.asarray([r["features"][key] for r in train if r["features"].get(key) is not None])
        b = np.asarray([r["features"][key] for r in recent if r["features"].get(key) is not None])
        if len(a) < 10 or len(b) < 10:
            out[key] = None; continue
        edges = np.unique(np.quantile(a, np.linspace(0, 1, 11)))
        if len(edges) < 3:
            out[key] = {"median_shift": float(np.median(b)-np.median(a)), "psi": None}; continue
        edges[0], edges[-1] = -np.inf, np.inf
        p = np.histogram(a, edges)[0]+.5; q = np.histogram(b, edges)[0]+.5
        p = p/p.sum(); q = q/q.sum()
        out[key] = {"psi": float(np.sum((q-p)*np.log(q/p))), "median_shift": float(np.median(b)-np.median(a))}
    return out
