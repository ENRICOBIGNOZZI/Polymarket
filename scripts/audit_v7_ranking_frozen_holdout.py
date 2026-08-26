#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class HorizonAudit:
    horizon_minutes: int
    holdout_start_ts: int
    observed_score_ts: int
    elapsed_seconds: int
    embargo_seconds: int
    horizon_seconds: int
    rolling_first_holdout_origin_eligible_eval_ts: int
    frozen_max_training_origin_ts: int
    rolling_max_training_origin_at_observed_score_ts: int
    holdout_training_can_enter_by_observed_score: bool


def audit_horizon(
    *,
    horizon_minutes: int,
    holdout_start_ts: int,
    observed_score_ts: int,
    fidelity_minutes: int,
    embargo_buckets: int,
) -> HorizonAudit:
    horizon_seconds = int(horizon_minutes) * 60
    embargo_seconds = int(fidelity_minutes) * 60 * int(embargo_buckets)
    # Current _rolling_section_metrics admits origin timestamps up to
    # eval_ts - embargo - horizon. A strict frozen holdout keeps this cutoff
    # fixed at the holdout boundary for every forward evaluation.
    rolling_max_origin = int(observed_score_ts) - embargo_seconds - horizon_seconds
    frozen_max_origin = int(holdout_start_ts) - embargo_seconds - horizon_seconds
    first_eligible_eval = int(holdout_start_ts) + embargo_seconds + horizon_seconds
    return HorizonAudit(
        horizon_minutes=int(horizon_minutes),
        holdout_start_ts=int(holdout_start_ts),
        observed_score_ts=int(observed_score_ts),
        elapsed_seconds=int(observed_score_ts) - int(holdout_start_ts),
        embargo_seconds=embargo_seconds,
        horizon_seconds=horizon_seconds,
        rolling_first_holdout_origin_eligible_eval_ts=first_eligible_eval,
        frozen_max_training_origin_ts=frozen_max_origin,
        rolling_max_training_origin_at_observed_score_ts=rolling_max_origin,
        holdout_training_can_enter_by_observed_score=(rolling_max_origin >= int(holdout_start_ts)),
    )


def build_report(config: dict[str, Any], observed: dict[str, Any]) -> dict[str, Any]:
    history = config["history"]
    discovery = config["discovery"]
    holdout_start = int(discovery["forward_holdout_start_ts"])
    horizons = []
    by_horizon = {int(row["horizon_minutes"]): row for row in observed.get("horizons", [])}
    for horizon_minutes in config["horizons_minutes"]:
        row = by_horizon.get(int(horizon_minutes), {})
        score_ts = int(row.get("score_timestamp") or observed.get("timestamp") or 0)
        horizons.append(
            asdict(
                audit_horizon(
                    horizon_minutes=int(horizon_minutes),
                    holdout_start_ts=holdout_start,
                    observed_score_ts=score_ts,
                    fidelity_minutes=int(history["fidelity_minutes"]),
                    embargo_buckets=int(history["purge_embargo_buckets"]),
                )
            )
        )
    return {
        "schema_version": 1,
        "decision": "MORE_EVIDENCE_REQUIRED",
        "finding": "frozen_holdout_training_cutoff_is_not_frozen",
        "contract": {
            "frozen_holdout_only": bool(config.get("frequency_registration", {}).get("frozen_holdout_only")),
            "horizons_minutes": list(config["horizons_minutes"]),
            "relative_pair_only": bool(config.get("relative_pair_contract", {}).get("relative_forecast_only")),
        },
        "current_evaluator": {
            "training_upper_origin_rule": "eval_ts - embargo_seconds - horizon_seconds",
            "effect": "as eval_ts advances, matured rows originating inside the holdout become eligible for refitting",
        },
        "strict_frozen_contract": {
            "training_upper_origin_rule": "holdout_start_ts - embargo_seconds - horizon_seconds",
            "effect": "the training sample and model parameters remain fixed for all holdout predictions",
        },
        "observed_forward_evidence": {
            "forward_days_observed": int(observed.get("forward_days_observed") or 0),
            "current_relative_pair_candidates": int(observed.get("current_relative_pair_candidates") or 0),
            "horizons": [
                {
                    "horizon_minutes": int(row.get("horizon_minutes") or 0),
                    "forward_cross_sections": int(row.get("forward_cross_sections") or 0),
                    "forward_gate": bool(row.get("forward_gate")),
                    "mean_daily_rank_ic": float((row.get("forward_blocked_inference") or {}).get("mean_daily_rank_ic") or 0.0),
                    "mean_daily_top_bottom_logit_spread": float((row.get("forward_blocked_inference") or {}).get("mean_daily_top_bottom_logit_spread") or 0.0),
                }
                for row in observed.get("horizons", [])
            ],
        },
        "horizon_audits": horizons,
        "promotion_blocker": (
            "The 2h/6h lane is configured as frozen_holdout_only, but the forward metrics are produced by a rolling "
            "refit whose training cutoff advances with eval_ts. Positive future results from this lane cannot be called "
            "frozen-holdout evidence until the fit is frozen at the registered holdout boundary."
        ),
        "successor_test": (
            "Fit each horizon once using labels available before holdout_start minus embargo; keep that fit fixed for all "
            "registered forward predictions. Keep the separate online observer walk-forward if desired, but do not pool it "
            "with the frozen 2h/6h holdout inference."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit whether the V7 2h/6h ranking holdout is truly frozen")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--observed", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    observed = json.loads(args.observed.read_text(encoding="utf-8"))
    report = build_report(config, observed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
