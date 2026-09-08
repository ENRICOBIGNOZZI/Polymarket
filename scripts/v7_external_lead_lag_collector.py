#!/usr/bin/env python3
"""Collect receive-time-causal external -> Polymarket lead/lag labels.

Research observer only.  It never authorizes trading.  Each origin is one frozen
rich external feature cut plus the exact PM prior snapshot used at that cut.
Labels are the first observed PM snapshots at/after 100/250/500/1000 ms.
"""
from __future__ import annotations

import argparse
from collections import deque
import hashlib
import json
import math
import os
from pathlib import Path
import time
from typing import Any

from v7_fair_model_artifact import canonical_hash

SCHEMA = "polymarket_v7_external_pm_lead_lag_observation_v1"
STATUS_SCHEMA = "polymarket_v7_external_pm_lead_lag_collector_status_v1"
HORIZONS_MS = (100, 250, 500, 1000)
MAX_LABEL_DELAY_MS = 50


def horizon_eligible(horizon: int, realized: float) -> bool:
    return horizon in HORIZONS_MS and math.isfinite(realized) and horizon <= realized <= horizon + MAX_LABEL_DELAY_MS


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()


def finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def logit(p: float) -> float:
    p = min(1 - 1e-9, max(1e-9, p))
    return math.log(p / (1 - p))


def valid_router_live(router: dict[str, Any], model_sha: str) -> dict[str, Any] | None:
    live = router.get("live_market") if isinstance(router.get("live_market"), dict) else {}
    value = finite(live.get("yes"))
    if (
        router.get("code_sha") != model_sha
        or router.get("paper_only") is not True
        or router.get("authenticated_execution") is not False
        or router.get("real_order_submission") is not False
        or live.get("valid") is not True
        or live.get("source") != "LIVE_COMPLEMENT_CONSISTENT_CLOB_BATCH"
        or value is None or not 0 <= value <= 1
        or not str(live.get("market_id") or "")
        or not str(live.get("snapshot_id") or "")
        or int(live.get("receive_ts_ms") or 0) <= 0
    ):
        return None
    return {
        "market_id": str(live["market_id"]), "yes": value,
        "snapshot_id": str(live["snapshot_id"]),
        "receive_ts_ms": int(live["receive_ts_ms"]),
        "exchange_ts_ms": int(live.get("exchange_ts_ms") or 0),
    }


def valid_origin(fair_status: dict[str, Any], live: dict[str, Any], model_sha: str) -> dict[str, Any] | None:
    fair = fair_status.get("fair") if isinstance(fair_status.get("fair"), dict) else {}
    market = fair_status.get("market") if isinstance(fair_status.get("market"), dict) else {}
    cut = fair.get("rich_feature_cut") if isinstance(fair.get("rich_feature_cut"), dict) else None
    features = fair.get("rich_model_features") if isinstance(fair.get("rich_model_features"), dict) else None
    if (
        fair_status.get("code_sha") != model_sha
        or fair_status.get("paper_only") is not True
        or fair_status.get("authenticated_execution") is not False
        or fair_status.get("real_order_submission") is not False
        or fair.get("paper_exploration_learned") is not True
        or fair.get("market_prior_causal_cut_valid") is not True
        or fair.get("uses_polymarket_price_as_feature") is not True
        or cut is None or features is None
        or str(market.get("market_id") or "") != live["market_id"]
    ):
        return None
    sha = str(fair.get("rich_feature_sha256") or "")
    if len(sha) != 64 or canonical_hash(cut) != sha:
        return None
    observed_ns = int(cut.get("observed_wall_ns") or 0)
    p0 = finite(cut.get("market_probability"))
    prior = cut.get("market_prior_snapshot") if isinstance(cut.get("market_prior_snapshot"), dict) else {}
    prior_snapshot_id = str(prior.get("snapshot_id") or fair.get("market_prior_snapshot_id") or "")
    prior_receive_ms = int(prior.get("receive_ts_ms") or fair.get("pm_mid_receive_ts_ms") or 0)
    prior_exchange_ms = int(prior.get("exchange_ts_ms") or fair.get("pm_mid_exchange_ts_ms") or 0)
    if (observed_ns <= 0 or p0 is None or not 0 <= p0 <= 1 or not prior_snapshot_id
            or prior_receive_ms <= 0 or prior_receive_ms * 1_000_000 > observed_ns):
        return None
    origin_id = hashlib.sha256(
        f"{model_sha}|{live['market_id']}|{sha}|{prior_snapshot_id}|{observed_ns}".encode()
    ).hexdigest()
    return {
        "origin_id": origin_id, "market_id": live["market_id"],
        "origin_observed_wall_ns": observed_ns, "origin_pm_yes": p0,
        "origin_pm_snapshot_id": prior_snapshot_id,
        "origin_pm_receive_ts_ms": prior_receive_ms,
        "origin_pm_exchange_ts_ms": prior_exchange_ms,
        "rich_feature_sha256": sha, "rich_model_features": features,
        "rich_feature_cut": cut, "labels": set(),
    }


class Collector:
    def __init__(self, fair_path: Path, router_path: Path, output: Path, status: Path,
                 model_sha: str, interval_ms: int = 25) -> None:
        self.fair_path, self.router_path = fair_path, router_path
        self.output, self.status, self.model_sha = output, status, model_sha
        self.interval_ms = max(10, min(250, interval_ms))
        self.pending: deque[dict[str, Any]] = deque(maxlen=10000)
        self.last_origin_id = ""
        self.last_router_snapshot_id = ""
        self.origins = self.labels = self.market_rollover_censors = self.invalid_reads = 0
        self.late_labels = 0
        self.started_ns = time.time_ns()

    def tick(self) -> None:
        router = load(self.router_path)
        live = valid_router_live(router, self.model_sha)
        if live is None:
            self.invalid_reads += 1
            return
        fair_status = load(self.fair_path)
        origin = valid_origin(fair_status, live, self.model_sha)
        if origin is not None and origin["origin_id"] != self.last_origin_id:
            self.pending.append(origin)
            self.last_origin_id = origin["origin_id"]
            self.origins += 1
        if live["snapshot_id"] == self.last_router_snapshot_id:
            return
        self.last_router_snapshot_id = live["snapshot_id"]
        keep: deque[dict[str, Any]] = deque(maxlen=10000)
        for row in self.pending:
            if row["market_id"] != live["market_id"]:
                self.market_rollover_censors += len(HORIZONS_MS) - len(row["labels"])
                continue
            origin_ms = row["origin_observed_wall_ns"] / 1_000_000.0
            for horizon in HORIZONS_MS:
                if horizon in row["labels"] or live["receive_ts_ms"] < origin_ms + horizon:
                    continue
                realized = live["receive_ts_ms"] - origin_ms
                eligible = horizon_eligible(horizon, realized)
                self.late_labels += int(not eligible)
                payload = {
                    "schema": SCHEMA, "paper_only": True, "authenticated_execution": False,
                    "real_order_submission": False, "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
                    "model_sha": self.model_sha, "origin_id": row["origin_id"],
                    "market_id": row["market_id"], "horizon_ms": horizon,
                    "realized_horizon_ms": realized,
                    "label_delay_ms": realized - horizon,
                    "maximum_label_delay_ms": MAX_LABEL_DELAY_MS,
                    "nominal_horizon_eligible": eligible,
                    "label_state": "OBSERVED_WITHIN_TOLERANCE" if eligible else "LATE_SNAPSHOT_CENSORED",
                    "target_semantics": "FIRST_OBSERVED_SNAPSHOT_AFTER_THRESHOLD",
                    "origin_observed_wall_ns": row["origin_observed_wall_ns"],
                    "origin_pm_yes": row["origin_pm_yes"],
                    "origin_pm_snapshot_id": row["origin_pm_snapshot_id"],
                    "origin_pm_receive_ts_ms": row["origin_pm_receive_ts_ms"],
                    "label_pm_yes": live["yes"], "label_pm_snapshot_id": live["snapshot_id"],
                    "label_pm_receive_ts_ms": live["receive_ts_ms"],
                    "label_pm_exchange_ts_ms": live["exchange_ts_ms"],
                    "delta_probability": live["yes"] - row["origin_pm_yes"],
                    "delta_logit": logit(live["yes"]) - logit(row["origin_pm_yes"]),
                    "rich_feature_sha256": row["rich_feature_sha256"],
                    "rich_model_features": row["rich_model_features"],
                }
                append_jsonl(self.output, payload)
                row["labels"].add(horizon)
                self.labels += 1
            if len(row["labels"]) < len(HORIZONS_MS):
                keep.append(row)
        self.pending = keep

    def publish(self) -> None:
        atomic_json(self.status, {
            "schema": STATUS_SCHEMA, "paper_only": True, "authenticated_execution": False,
            "real_order_submission": False, "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
            "model_sha": self.model_sha, "started_ns": self.started_ns, "timestamp_ns": time.time_ns(),
            "origins": self.origins, "labels": self.labels, "pending_origins": len(self.pending),
            "market_rollover_censors": self.market_rollover_censors, "invalid_reads": self.invalid_reads,
            "horizons_ms": list(HORIZONS_MS), "interval_ms": self.interval_ms,
            "late_labels": self.late_labels,
            "nominal_horizon_eligible_labels": self.labels - self.late_labels,
            "maximum_label_delay_ms": MAX_LABEL_DELAY_MS,
            "state": "COLLECTING" if self.origins else "AWAITING_CAUSAL_RICH_FEATURE_CUT",
        })

    def run(self) -> None:
        next_status = 0.0
        while True:
            self.tick()
            now = time.monotonic()
            if now >= next_status:
                self.publish(); next_status = now + 1.0
            time.sleep(self.interval_ms / 1000.0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fair-status", type=Path, required=True)
    ap.add_argument("--router-status", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--status", type=Path, required=True)
    ap.add_argument("--model-sha", required=True)
    ap.add_argument("--interval-ms", type=int, default=25)
    args = ap.parse_args()
    if len(args.model_sha) != 40 or any(c not in "0123456789abcdef" for c in args.model_sha):
        raise SystemExit("invalid --model-sha")
    Collector(args.fair_status, args.router_status, args.output, args.status,
              args.model_sha, args.interval_ms).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
