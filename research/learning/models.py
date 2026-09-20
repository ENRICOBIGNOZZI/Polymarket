"""Small structured baselines and offline challengers with a PM logit offset."""
from __future__ import annotations

import warnings
import numpy as np
from scipy.optimize import minimize
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.isotonic import IsotonicRegression

from .common import ASSETS, HORIZONS
from .validation import sigmoid, logit, weights, calibration_fit

FAMILIES = ("pm", "logistic_offset", "boosted_offset")


class Features:
    """Train-only robust scaling; missingness is explicit, raw nulls preserved."""
    def fit(self, rows):
        names = sorted({k for r in rows for k, v in r["features"].items() if v is not None
                        and k not in {"pm_probability", "market_logit"}})
        self.names = names
        self.median = []; self.scale = []
        for name in names:
            values = [float(r["features"][name]) for r in rows if r["features"].get(name) is not None]
            if not np.isfinite(values).all():
                raise ValueError("NONFINITE_TRAIN_FEATURE")
            self.median.append(float(np.median(values)))
            q1, q3 = np.quantile(values, [.25, .75]); self.scale.append(float(q3-q1) or 1.)
        return self

    def transform(self, rows):
        out = []
        for r in rows:
            values = [r["features"].get(k) for k in self.names]
            # Explicit train-median imputation plus indicators, never source zeros.
            out.append([np.clip(((float(v) if v is not None else m)-m)/s, -20, 20)
                        for v, m, s in zip(values, self.median, self.scale)]
                       + [float(v is None) for v in values])
        return np.asarray(out, dtype=float).reshape(len(rows), 2*len(self.names))


def offset_logistic(X, y, w, offset, ridge):
    penalty = np.broadcast_to(ridge, X.shape[1])
    def objective(b):
        z = offset+X@b
        loss = np.sum(w*(np.logaddexp(0, z)-y*z)) + .5*np.sum(penalty*b*b)
        gradient = X.T@(w*(sigmoid(z)-y)) + penalty*b
        return loss, gradient
    fit = minimize(objective, np.zeros(X.shape[1]), jac=True, method="L-BFGS-B",
                   options={"maxiter": 1000, "ftol": 1e-12, "gtol": 1e-8})
    if not fit.success:
        raise ValueError("MODEL_OPTIMIZER_DID_NOT_CONVERGE")
    return fit.x


class Candidate:
    def __init__(self, family, ridge=8., weighting="market", seed=20260920):
        if family not in FAMILIES:
            raise ValueError("UNKNOWN_MODEL_FAMILY")
        self.family, self.ridge, self.weighting, self.seed = family, ridge, weighting, seed

    def matrix(self, rows, fit=False):
        if fit:
            self.features = Features().fit(rows)
        X = self.features.transform(rows)
        # Shared correction with regularized context effects; no 30 independent fits.
        groups = np.asarray([[float(r["asset"] == a) for a in ASSETS]
                             + [float(r["horizon"] == h) for h in HORIZONS] for r in rows])
        return np.column_stack([np.ones(len(rows)), X, groups])

    def fit(self, rows, cutoff_ns):
        if not rows or any(r["label_information_ns"] >= cutoff_ns or r["outcome"] not in (0, 1) for r in rows):
            raise ValueError("TRAINING_LABEL_UNAVAILABLE")
        self.cutoff_ns = cutoff_ns
        X = self.matrix(rows, fit=True); y = np.asarray([r["outcome"] for r in rows])
        w = weights(rows, self.weighting, cutoff_ns=cutoff_ns)
        if w.sum() <= 0:
            raise ValueError("NO_WEIGHTED_TRAINING_INFORMATION")
        p = np.asarray([r["pm_probability"] for r in rows]); offset = logit(p)
        self.estimator = None; self.beta = np.zeros(X.shape[1])
        if self.family == "pm":
            return self
        if self.family == "logistic_offset":
            self.beta = offset_logistic(X, y, w, offset, self.ridge)
        else:
            # One Newton residual step about PM. Bound rare-tail influence.
            target = np.clip((y-p)/np.maximum(p*(1-p), .02), -8, 8)
            residual_weights = w*np.maximum(p*(1-p), .02)
            self.estimator = HistGradientBoostingRegressor(max_iter=60, max_leaf_nodes=7,
                l2_regularization=self.ridge, early_stopping=False, random_state=self.seed)
            with warnings.catch_warnings():
                from sklearn.exceptions import ConvergenceWarning
                warnings.simplefilter("error", ConvergenceWarning)
                self.estimator.fit(X, target, sample_weight=residual_weights)
        return self

    def predict(self, rows):
        if any(r["decision_ns"] < self.cutoff_ns for r in rows):
            raise ValueError("PREDICTION_PRECEDES_MODEL_AVAILABILITY")
        return self.predict_cut(rows)

    def predict_cut(self, rows):
        X = self.matrix(rows)
        correction = self.estimator.predict(X) if self.estimator is not None else X@self.beta
        return sigmoid(logit(np.asarray([r["pm_probability"] for r in rows])) + correction)

    def parameters(self):
        value = {"family": self.family, "ridge": self.ridge, "weighting": self.weighting,
                 "seed": self.seed, "cutoff_ns": self.cutoff_ns, "feature_names": self.features.names,
                 "median": self.features.median, "scale": self.features.scale,
                 "coefficients": self.beta.tolist(), "pm_logit_coefficient": 1.,
                 "asset_order": list(ASSETS), "horizon_order": list(HORIZONS),
                 "missingness": "TRAIN_MEDIAN_AND_EXPLICIT_INDICATOR"}
        if self.estimator is not None:
            value["baseline"] = self.estimator._baseline_prediction.tolist()
            value["trees"] = [[{k: node[k].item() for k in tree.nodes.dtype.names} for node in tree.nodes]
                              for stage in self.estimator._predictors for tree in stage]
            value["tree_serialization"] = "SKLEARN_VERSION_BOUND_OFFLINE_ONLY"
        return value


def calibrate(p, rows, method):
    y = np.asarray([r["outcome"] for r in rows]); w = weights(rows)
    if method == "raw":
        return {"method": method}
    if method in {"platt", "temperature"}:
        return {"method": method, **calibration_fit(p, y, w, temperature=method == "temperature")}
    if method == "isotonic":
        model = IsotonicRegression(out_of_bounds="clip").fit(p, y, sample_weight=w)
        return {"method": method, "x": model.X_thresholds_.tolist(), "y": model.y_thresholds_.tolist()}
    raise ValueError("UNKNOWN_CALIBRATION_METHOD")


def calibrated(p, params):
    if params["method"] == "raw":
        return p
    if params["method"] == "isotonic":
        return np.interp(p, params["x"], params["y"])
    return sigmoid(params["intercept"]+params["slope"]*logit(p))
