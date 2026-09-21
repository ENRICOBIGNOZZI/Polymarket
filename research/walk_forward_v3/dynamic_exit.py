"""Research-only dynamic exit value model for held Polymarket positions.

Learns the incremental value of HOLD versus EXIT_NOW from a refreshed causal
state. Entry cost is sunk: the target compares future executable bid proceeds
with executable proceeds available now. No trading authority or promotion.
"""
from __future__ import annotations

from collections import Counter
import argparse
import math
from pathlib import Path

from research.walk_forward_v2.core import (
    SAFETY, atomic_json, build_dataset, fee_per_share, finite,
)
from research.walk_forward_v3.direct_action import (
    DEFAULT_ACTION_HORIZONS_MS,
    DirectActionValueModel,
    StreamingRidge,
    decision_action_sides,
    decision_side_state,
    observed_side_state,
)

SCHEMA = "polymarket_dynamic_exit_value_v1"
DEFAULT_DYNAMIC_EXIT_HORIZONS_MS = tuple(
    h for h in DEFAULT_ACTION_HORIZONS_MS if h >= 250
)


def _quantile(values, level):
    values = sorted(float(v) for v in values if finite(v))
    if not values:
        return None
    index = int(math.ceil(float(level) * len(values))) - 1
    return values[max(0, min(len(values) - 1, index))]


def hold_incremental_value(row, *, side, horizon_ms, size=1.0):
    """Executable incremental cash value of holding instead of exiting now."""
    current = decision_side_state(row, side)
    if current is None:
        return None, "CURRENT_SIDE_STATE_UNAVAILABLE"
    target = (row.get("targets") or {}).get(str(int(horizon_ms))) or {}
    if target.get("state") != "OBSERVED":
        return None, "FUTURE_EXIT_EVIDENCE_UNAVAILABLE"
    future = observed_side_state(target, side, row)
    if future is None:
        return None, "FUTURE_SIDE_STATE_UNAVAILABLE"

    size = float(size)
    if not finite(size) or size <= 0:
        return None, "INVALID_POSITION_SIZE"
    current_depth = max(0.0, float(current.get("bid_quantity") or 0.0))
    future_depth = max(0.0, float(future.get("bid_quantity") or 0.0))
    if current_depth + 1e-12 < size:
        return None, "CURRENT_EXIT_DEPTH_INSUFFICIENT"
    if future_depth + 1e-12 < size:
        return None, "FUTURE_EXIT_DEPTH_INSUFFICIENT"

    current_bid = float(current["bid"])
    future_bid = float(future["bid"])
    current_fee = fee_per_share(row, current_bid)
    future_fee = fee_per_share(row, future_bid)
    current_net = current_bid - current_fee
    future_net = future_bid - future_fee
    return float(size * (future_net - current_net)), "OBSERVED"


class DynamicExitValueModel:
    """Q_hold(S, side, h): lower-bound incremental value of waiting."""

    def __init__(
        self,
        *,
        horizons_ms=DEFAULT_DYNAMIC_EXIT_HORIZONS_MS,
        ridge=8.0,
        calibration_level=0.90,
        streaming_batch_size=4096,
    ):
        self.horizons_ms = tuple(sorted({int(h) for h in horizons_ms}))
        self.ridge = float(ridge)
        self.calibration_level = float(calibration_level)
        self.streaming_batch_size = int(streaming_batch_size)
        if not self.horizons_ms or self.horizons_ms[0] <= 0:
            raise ValueError("positive dynamic-exit horizons required")
        if not finite(self.ridge) or self.ridge <= 0:
            raise ValueError("positive dynamic-exit ridge required")
        if not 0.5 <= self.calibration_level < 1:
            raise ValueError("dynamic-exit calibration level must be in [0.5,1)")
        self.fitted = False

    def _feature_builder(self, rows):
        builder = DirectActionValueModel(
            size_grid=(1.0,),
            action_horizons_ms=self.horizons_ms,
            train_latencies_ms=(0,),
            selection_calibration_mode="OFF",
            max_sizes_per_state=1,
        )
        builder._configure_levels(rows)
        return builder

    def _iter_records(self, rows, *, markets=None, counter=None, cell_counter=None):
        allowed = set(markets) if markets is not None else None
        for row in rows:
            if allowed is not None and str(row.get("market_id")) not in allowed:
                continue
            for side in decision_action_sides(row):
                for horizon in self.horizons_ms:
                    value, state = hold_incremental_value(
                        row, side=side, horizon_ms=horizon, size=1.0)
                    if counter is not None:
                        counter[state] += 1
                    if value is None:
                        continue
                    if cell_counter is not None:
                        cell_counter[f"{side}::{horizon}"] += 1
                    record = self.builder._action_record(
                        row, size=1.0, horizon_ms=horizon,
                        latency_ms=0, side=side)
                    record["target"] = float(value)
                    record["target_state"] = state
                    yield record

    def fit(self, rows):
        rows = list(rows)
        if not rows:
            raise ValueError("dynamic-exit training rows required")
        self.builder = self._feature_builder(rows)
        markets = DirectActionValueModel._market_order(rows)
        if not markets:
            raise ValueError("no admissible dynamic-exit markets")

        cut = max(1, int(len(markets) * 0.80))
        if cut >= len(markets):
            fit_markets = set(markets)
            calibration_markets = set()
        else:
            fit_markets = set(markets[:cut])
            calibration_markets = set(markets[cut:])

        states = Counter()
        cells = Counter()
        self.mean_model = StreamingRidge(
            self.builder.model_feature_names,
            ridge=self.ridge,
            batch_size=self.streaming_batch_size,
        ).fit_factory(
            lambda: self._iter_records(
                rows, markets=fit_markets, counter=states,
                cell_counter=cells),
            lambda record: record["target"],
        )

        block_scores = {}
        if calibration_markets:
            for record in self._iter_records(rows, markets=calibration_markets):
                error = abs(
                    float(record["target"]) - self.mean_model.predict(record))
                market = str(record["market_id"])
                block_scores[market] = max(error, block_scores.get(market, 0.0))
        margin = _quantile(block_scores.values(), self.calibration_level)
        self.calibration_margin_per_share = (
            float(max(0.0, margin))
            if margin is not None else float(max(0.0, self.mean_model.target_std))
        )
        self.cell_target_counts = dict(cells)
        self.training_receipt = {
            "schema": SCHEMA + "_training_v1",
            **SAFETY,
            "state": "READY",
            "policy": "HOLD_VS_EXIT_NOW_INCREMENTAL_EXECUTABLE_VALUE",
            "entry_cost_semantics": "SUNK_NOT_RECHARGED",
            "horizons_ms": list(self.horizons_ms),
            "training_markets": len(fit_markets),
            "calibration_markets": len(calibration_markets),
            "training_targets": int(self.mean_model.rows),
            "target_state_counts": dict(states),
            "cell_target_counts": dict(sorted(self.cell_target_counts.items())),
            "calibration_level": self.calibration_level,
            "calibration_margin_per_share": self.calibration_margin_per_share,
            "re_evaluation": "ON_EVERY_CAUSAL_BOOK_UPDATE",
            "automatic_promotion": False,
        }
        self.fitted = True
        return self

    def decide(
        self, row, *, side, position_size,
        minimum_incremental_value=0.0,
    ):
        if not self.fitted:
            raise RuntimeError("dynamic exit model not fitted")
        position_size = float(position_size)
        current = decision_side_state(row, side)
        if current is None:
            return {"action": "CENSORED", "reason": "CURRENT_SIDE_STATE_UNAVAILABLE"}
        if float(current.get("bid_quantity") or 0.0) + 1e-12 < position_size:
            return {"action": "CENSORED", "reason": "CURRENT_EXIT_DEPTH_INSUFFICIENT"}

        candidates = []
        for horizon in self.horizons_ms:
            if int(self.cell_target_counts.get(f"{side}::{horizon}", 0)) <= 0:
                continue
            record = self.builder._action_record(
                row, size=1.0, horizon_ms=horizon, latency_ms=0, side=side)
            predicted_per_share = float(self.mean_model.predict(record))
            lower_per_share = (
                predicted_per_share - self.calibration_margin_per_share)
            candidates.append({
                "additional_horizon_ms": int(horizon),
                "predicted_incremental_value_per_share": predicted_per_share,
                "lower_incremental_value_per_share": lower_per_share,
                "predicted_incremental_value": predicted_per_share * position_size,
                "lower_incremental_value": lower_per_share * position_size,
            })

        if not candidates:
            return {"action": "EXIT_NOW", "reason": "NO_SUPPORTED_HOLD_HORIZON"}
        candidates.sort(
            key=lambda item: (
                item["lower_incremental_value"],
                item["predicted_incremental_value"],
                -item["additional_horizon_ms"],
            ),
            reverse=True,
        )
        best = candidates[0]
        if best["lower_incremental_value"] <= float(minimum_incremental_value):
            return {
                "action": "EXIT_NOW",
                "reason": "HOLD_LOWER_VALUE_NONPOSITIVE",
                "best_hold_candidate": best,
                "re_evaluate_on_next_causal_book_update": True,
            }
        return {
            "action": "HOLD",
            "reason": "DYNAMIC_HOLD_VALUE_POSITIVE",
            **best,
            "re_evaluate_on_next_causal_book_update": True,
        }



def summarize_dynamic_exit(model, rows, *, position_size=1.0):
    counts=Counter()
    observed=0
    realized=0.0
    for row in rows:
        for side in decision_action_sides(row):
            decision=model.decide(
                row, side=side, position_size=position_size)
            action=str(decision.get("action") or "UNKNOWN")
            counts[action]+=1
            if action!="HOLD":
                continue
            horizon=int(decision["additional_horizon_ms"])
            value,state=hold_incremental_value(
                row,side=side,horizon_ms=horizon,size=position_size)
            if value is not None:
                observed+=1
                realized+=float(value)
            else:
                counts["HOLD_CENSORED_"+str(state)]+=1
    return {
        "schema":SCHEMA+"_diagnostic_v1",
        **SAFETY,
        "diagnostic_only":True,
        "automatic_promotion":False,
        "position_size":float(position_size),
        "decisions":dict(sorted(counts.items())),
        "observed_hold_outcomes":observed,
        "realized_incremental_hold_value":realized,
    }


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--minimum-wall-ns",type=int,required=True)
    a=p.parse_args(argv)
    data=build_dataset(
        a.root,minimum_wall_ns=a.minimum_wall_ns,
        include_settlement_labels=False,use_compact_window_index=True)
    if data.get("input_state")!="READY":
        out={"schema":SCHEMA,**SAFETY,"state":data.get("input_state")}
        atomic_json(a.output,out)
        return 2
    rows=data["decisions"]
    model=DynamicExitValueModel().fit(rows)
    out={
        "schema":SCHEMA,
        **SAFETY,
        "state":"READY",
        "automatic_promotion":False,
        "training_receipt":model.training_receipt,
        "diagnostic":summarize_dynamic_exit(model,rows),
        "data_sha256":data.get("data_sha256"),
    }
    atomic_json(a.output,out)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
