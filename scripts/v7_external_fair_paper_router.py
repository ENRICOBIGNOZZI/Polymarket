#!/usr/bin/env python3
"""Collect settlement-aware External Fair counterfactuals without execution.

Only public CLOB data is used.  Every decision is revalidated on a fresh L2
arrival snapshot, fees are taken from the contract-bound schedule, and virtual
FAK fills are limited by visible depth. Evidence is written to an append-only
counterfactual tape and the canonical ledger with explicit SHADOW authority.
It never reaches portfolio cash or authoritative PAPER PnL.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from v7_market_common import finite, parse_array, request_json
from v7_execution_ledger import LedgerEvent
from v7_ledger_spool import spool_event

STRATEGY = "CRYPTO_INFORMED_TAKER"
MODEL_VERSION = "external-fair-structural-v7-paper"
# Evidence produced by the same declared semantics may survive an exact-SHA
# cutover.  Bump this whenever forecast labels, settlement semantics or virtual
# execution economics change incompatibly.  Exact SHA still governs execution;
# this version governs only read-only SHADOW evidence pooling.
EVIDENCE_SEMANTICS_VERSION = "external-fair-settlement-evidence-v1"
HORIZONS = (1, 10, 45, 60, 300)
FORECAST_TTE_BUCKETS = (240, 180, 120, 90, 60, 45, 30, 20, 15, 10, 5)
FORECAST_BUCKET_TOLERANCE_SECONDS = 1.25
MAX_CLOB_CLOCK_SKEW_MS = 250


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def stable_id(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()[:32]


def expected_calibration_error(
    predictions: list[float], actuals: list[float], *, bins: int = 10,
) -> float | None:
    """Fixed-bin ECE over independent settlement-cluster observations."""
    pairs = [
        (float(prediction), float(actual))
        for prediction, actual in zip(predictions, actuals)
        if math.isfinite(prediction) and math.isfinite(actual)
        and 0.0 <= prediction <= 1.0 and 0.0 <= actual <= 1.0
    ]
    if not pairs:
        return None
    groups: list[list[tuple[float, float]]] = [
        [] for _ in range(max(1, int(bins)))
    ]
    for prediction, actual in pairs:
        index = min(len(groups) - 1, int(prediction * len(groups)))
        groups[index].append((prediction, actual))
    return sum(
        len(group) / len(pairs)
        * abs(statistics.fmean(p for p, _ in group)
              - statistics.fmean(y for _, y in group))
        for group in groups if group
    )


def logistic_calibration_line(
    predictions: list[float], actuals: list[float], *, ridge: float = 1e-6,
    max_iter: int = 50,
) -> tuple[float | None, float | None]:
    """Fit outcome ~ intercept + slope*logit(prediction) by logistic MLE."""
    pairs = [
        (min(1.0 - 1e-9, max(1e-9, float(prediction))), int(actual))
        for prediction, actual in zip(predictions, actuals)
        if math.isfinite(prediction) and math.isfinite(actual)
        and 0.0 <= prediction <= 1.0 and actual in (0.0, 1.0)
    ]
    if len(pairs) < 2 or len({actual for _, actual in pairs}) < 2:
        return None, None
    intercept, slope = 0.0, 1.0
    for _ in range(max(1, int(max_iter))):
        g0, g1 = ridge * intercept, ridge * (slope - 1.0)
        h00, h01, h11 = ridge, 0.0, ridge
        for probability, actual in pairs:
            x = math.log(probability / (1.0 - probability))
            z = max(-35.0, min(35.0, intercept + slope * x))
            fitted = 1.0 / (1.0 + math.exp(-z))
            error = fitted - actual
            variance = max(1e-9, fitted * (1.0 - fitted))
            g0 += error
            g1 += error * x
            h00 += variance
            h01 += variance * x
            h11 += variance * x * x
        determinant = h00 * h11 - h01 * h01
        if abs(determinant) < 1e-12:
            return None, None
        delta_intercept = (h11 * g0 - h01 * g1) / determinant
        delta_slope = (-h01 * g0 + h00 * g1) / determinant
        intercept -= delta_intercept
        slope = max(0.01, min(10.0, slope - delta_slope))
        if abs(delta_intercept) + abs(delta_slope) < 1e-9:
            break
    return intercept, slope


def _wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    if trials <= 0:
        return math.nan, math.nan
    proportion = successes / trials
    denominator = 1.0 + z * z / trials
    centre = (proportion + z * z / (2.0 * trials)) / denominator
    radius = z / denominator * math.sqrt(
        proportion * (1.0 - proportion) / trials
        + z * z / (4.0 * trials * trials))
    return max(0.0, centre - radius), min(1.0, centre + radius)


def probability_interval_bin_diagnostics(
    predictions: list[float], actuals: list[float],
    lowers: list[float], uppers: list[float], *, bins: int = 10,
    minimum_bin_size: int = 3,
) -> dict[str, Any]:
    """Validate epistemic probability bands against independent bin rates.

    A Bernoulli realization is always 0 or 1, so asking whether it lies inside
    a fair-probability band is mathematically invalid. We instead compare each
    bin's mean model band with the Wilson interval for its empirical event rate.
    """
    groups: list[list[tuple[float, float, float, float]]] = [
        [] for _ in range(max(1, int(bins)))
    ]
    for prediction, actual, lower, upper in zip(
        predictions, actuals, lowers, uppers
    ):
        values = (prediction, actual, lower, upper)
        if (
            not all(math.isfinite(value) for value in values)
            or not 0.0 <= lower <= prediction <= upper <= 1.0
            or actual not in (0.0, 1.0)
        ):
            continue
        index = min(len(groups) - 1, int(prediction * len(groups)))
        groups[index].append(values)
    rows: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        if len(group) < max(1, int(minimum_bin_size)):
            continue
        mean_prediction = statistics.fmean(row[0] for row in group)
        mean_lower = statistics.fmean(row[2] for row in group)
        mean_upper = statistics.fmean(row[3] for row in group)
        successes = sum(int(row[1]) for row in group)
        observed_rate = successes / len(group)
        observed_lower, observed_upper = _wilson_interval(successes, len(group))
        consistent = mean_lower <= observed_upper and mean_upper >= observed_lower
        rows.append({
            "bin": index,
            "markets": len(group),
            "mean_prediction": mean_prediction,
            "mean_lower": mean_lower,
            "mean_upper": mean_upper,
            "observed_rate": observed_rate,
            "observed_rate_wilson95_lower": observed_lower,
            "observed_rate_wilson95_upper": observed_upper,
            "probability_band_consistent": consistent,
        })
    return {
        "eligible_bin_count": len(rows),
        "consistency_rate": (
            sum(int(row["probability_band_consistent"]) for row in rows) / len(rows)
            if rows else None),
        "mean_probability_band_width": (
            statistics.fmean(row["mean_upper"] - row["mean_lower"] for row in rows)
            if rows else None),
        "bins": rows,
    }


def fee_per_share(price: float, schedule: dict[str, Any], *, taker: bool = True) -> float:
    if not 0.0 < price < 1.0:
        return math.inf
    try:
        rate = float(schedule["rate"])
        exponent = float(schedule["exponent"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return math.inf
    if not math.isfinite(rate) or not math.isfinite(exponent) or rate < 0.0 or exponent < 0.0:
        return math.inf
    if not taker and bool(schedule.get("takerOnly", True)):
        return 0.0
    return rate * (price * (1.0 - price)) ** exponent


def entry_tte_allowed(fair: dict[str, Any], policy: dict[str, Any]) -> bool:
    """Fail closed unless the forecast is inside the configured entry window."""
    tte = finite(fair.get("tte_seconds"))
    minimum = finite(policy.get("minimum_entry_tte_seconds"))
    maximum = finite(policy.get("maximum_entry_tte_seconds"))
    legacy_allowed = (
        math.isfinite(tte)
        and math.isfinite(minimum)
        and math.isfinite(maximum)
        and 0.0 <= minimum <= maximum
        and minimum <= tte <= maximum
    )
    buckets = policy.get("tte_bucket_policy")
    if not isinstance(buckets, list) or not buckets:
        return legacy_allowed
    return any(
        isinstance(bucket, dict)
        and finite(bucket.get("minimum_seconds"), math.nan) <= tte
        <= finite(bucket.get("maximum_seconds"), math.nan)
        for bucket in buckets
    )


def tte_policy(fair: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    tte = finite(fair.get("tte_seconds"), math.nan)
    for bucket in policy.get("tte_bucket_policy") if isinstance(policy.get("tte_bucket_policy"), list) else []:
        if not isinstance(bucket, dict):
            continue
        minimum = finite(bucket.get("minimum_seconds"), math.nan)
        maximum = finite(bucket.get("maximum_seconds"), math.nan)
        if math.isfinite(tte) and minimum <= tte <= maximum:
            return bucket
    return {}


def model_market_disagreement_allowed(
    fair: dict[str, Any],
    policy: dict[str, Any],
    market_yes: float | None = None,
) -> bool:
    """Reject uncalibrated model forecasts that radically contradict the market.

    The market is the benchmark the external model must beat, not an input that
    can be ignored.  Until forward calibration proves otherwise, a large gap is
    evidence of model/semantic risk rather than executable alpha.
    """
    model_yes = finite(fair.get("yes"))
    market_yes = finite(fair.get("pm_mid")) if market_yes is None else market_yes
    maximum = finite(policy.get("maximum_model_market_disagreement"))
    return (
        math.isfinite(model_yes)
        and math.isfinite(market_yes)
        and math.isfinite(maximum)
        and 0.0 <= model_yes <= 1.0
        and 0.0 <= market_yes <= 1.0
        and 0.0 <= maximum <= 1.0
        and abs(model_yes - market_yes) <= maximum
    )


def live_market_yes(books: dict[str, "Book"], market: dict[str, Any]) -> float | None:
    """Return a complement-consistent YES midpoint from one live book batch.

    Gamma's market midpoint is an opening/discovery snapshot and can remain
    unchanged while a five-minute CLOB moves from 50c to 99c. Execution and
    forecast benchmarks must therefore use the same arrival books that a PAPER
    FAK would face. The intersection of direct YES and complement-implied NO
    bounds also rejects crossed or semantically mismatched token books.
    """
    yes = books.get(str(market.get("yes_token") or ""))
    no = books.get(str(market.get("no_token") or ""))
    if yes is None or no is None or not (yes.bids and yes.asks and no.bids and no.asks):
        return None
    lower = max(yes.bids[0][0], 1.0 - no.asks[0][0])
    upper = min(yes.asks[0][0], 1.0 - no.bids[0][0])
    tolerance = max(yes.tick_size, no.tick_size, 1e-6)
    if lower > upper + tolerance:
        return None
    lower = max(0.0, min(1.0, lower))
    upper = max(lower, min(1.0, upper))
    return 0.5 * (lower + upper)


def hybrid_probability(external_yes: float, market_yes: float, weight: float) -> float:
    external = min(1.0 - 1e-9, max(1e-9, external_yes))
    market = min(1.0 - 1e-9, max(1e-9, market_yes))
    bounded_weight = min(1.0, max(0.0, weight))
    external_logit = math.log(external / (1.0 - external))
    market_logit = math.log(market / (1.0 - market))
    value = external_logit + bounded_weight * (market_logit - external_logit)
    return 1.0 / (1.0 + math.exp(-value)) if value >= 0.0 else math.exp(value) / (1.0 + math.exp(value))


@dataclass(frozen=True)
class Book:
    token_id: str
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    tick_size: float
    min_order_size: float
    exchange_ts_ms: int
    receive_ts_ms: int
    snapshot_id: str


def parse_book(raw: Any, receive_ts_ms: int) -> Book | None:
    if not isinstance(raw, dict):
        return None
    bids: list[tuple[float, float]] = []
    asks: list[tuple[float, float]] = []
    for key, output in (("bids", bids), ("asks", asks)):
        for row in raw.get(key) if isinstance(raw.get(key), list) else []:
            if not isinstance(row, dict):
                continue
            price, size = finite(row.get("price")), finite(row.get("size"), 0.0)
            if math.isfinite(price) and 0.0 < price < 1.0 and size > 0.0:
                output.append((price, size))
    bids.sort(reverse=True)
    asks.sort()
    token = str(raw.get("asset_id") or "")
    exchange = int(finite(raw.get("timestamp"), 0.0))
    if exchange and exchange < 10_000_000_000:
        exchange *= 1000
    if (not token or (not bids and not asks) or exchange <= 0
            or exchange > receive_ts_ms + MAX_CLOB_CLOCK_SKEW_MS):
        return None
    # The public CLOB clock can lead the local host by a few milliseconds.  A
    # bounded skew is safe to accept, but the canonical ledger clock must stay
    # causal (exchange <= receive).  Larger future timestamps still fail closed.
    exchange = min(exchange, receive_ts_ms)
    snapshot = str(raw.get("hash") or "") or stable_id(token, exchange, bids, asks)
    return Book(
        token, tuple(bids), tuple(asks), max(1e-6, finite(raw.get("tick_size"), 0.01)),
        max(1.0, finite(raw.get("min_order_size"), 1.0)), exchange, receive_ts_ms, snapshot,
    )


def robust_candidates(status: dict[str, Any], books: dict[str, Book], policy: dict[str, Any]) -> list[dict[str, Any]]:
    if status.get("paper_only") is not True or status.get("authenticated_execution") is not False:
        return []
    if status.get("real_order_submission") is not False:
        return []
    contract, reference = status.get("contract") or {}, status.get("settlement_reference") or {}
    oracle, external, fair, market = (
        status.get("oracle") or {}, status.get("external") or {}, status.get("fair") or {}, status.get("market") or {},
    )
    if not (contract.get("verified") and contract.get("rules_hash_recognized") and reference.get("valid")
            and oracle.get("healthy") and oracle.get("continuity") != "CONTINUITY_UNKNOWN"
            and external.get("healthy") and fair.get("valid")):
        return []
    if not entry_tte_allowed(fair, policy):
        return []
    market_yes = live_market_yes(books, market)
    if market_yes is None or not model_market_disagreement_allowed(
        fair, policy, market_yes
    ):
        return []
    calculated = int(fair.get("calculated_monotonic_ns") or 0)
    valid_until = int(fair.get("valid_until_monotonic_ns") or 0)
    current = time.monotonic_ns()
    if calculated <= 0 or calculated > current or valid_until < current:
        return []
    schedule = market.get("fee_schedule") if isinstance(market.get("fee_schedule"), dict) else {}
    bucket = tte_policy(fair, policy)
    if bucket and bucket.get("action") != "TAKER_SHADOW":
        return []
    minimum_ev = float(bucket.get("minimum_robust_ev_per_share", policy.get("minimum_robust_ev_per_share", 0.001)))
    execution_risk = float(bucket.get("execution_risk_per_share", policy.get("base_execution_risk_per_share", 0.0005)))
    rows: list[dict[str, Any]] = []
    for outcome, token, robust_value in (
        ("YES", str(market.get("yes_token") or ""), float(fair.get("lower") or 0.0)),
        ("NO", str(market.get("no_token") or ""), 1.0 - float(fair.get("upper") or 1.0)),
    ):
        book = books.get(token)
        if book is None or not book.asks:
            continue
        ask = book.asks[0][0]
        fee = fee_per_share(ask, schedule)
        robust_ev = robust_value - ask - fee - execution_risk
        if math.isfinite(robust_ev) and robust_ev >= minimum_ev:
            rows.append({
                "outcome": outcome, "token_id": token, "book": book, "ask": ask,
                "fee_per_share": fee, "execution_risk": execution_risk,
                "robust_probability": robust_value, "robust_ev": robust_ev,
                "market_yes": market_yes,
                "tte_seconds": float(fair["tte_seconds"]),
                "tte_bucket_id": str(bucket.get("id") or "legacy_entry_window"),
            })
    return sorted(rows, key=lambda row: (-row["robust_ev"], row["outcome"]))


def executable_sell_value(book: Book, shares: float, schedule: dict[str, Any]) -> float:
    """Return a full-depth liquidation value net of authoritative exit fees."""
    remaining = max(0.0, shares)
    value = 0.0
    for price, available in book.bids:
        quantity = min(remaining, available)
        value += quantity * max(0.0, price - fee_per_share(price, schedule))
        remaining -= quantity
        if remaining <= 1e-9:
            return value
    return 0.0


class PaperRouter:
    def __init__(self, run_root: Path, model_sha: str, config_path: Path, clob_url: str, gamma_url: str):
        self.root = run_root
        self.directory = run_root / "external_fair"
        self.sha = model_sha
        self.config = load(config_path)
        if (self.config.get("execution_authority") != "SHADOW_ZERO_AUTHORITY"
                or self.config.get("paper_only") is not True
                or self.config.get("authenticated_execution") is not False
                or self.config.get("real_order_submission") is not False):
            raise RuntimeError("external_fair_shadow_contract_invalid")
        self.policy = self.config.get("taker") if isinstance(self.config.get("taker"), dict) else {}
        if (self.policy.get("enabled_for_execution") is not False
                or self.policy.get("authority") != "SHADOW"
                or self.policy.get("counterfactual_enabled") is not True):
            raise RuntimeError("external_fair_taker_not_shadow_authorized")
        self.policy_sha256 = hashlib.sha256(
            json.dumps(self.config, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        fair_policy = self.config.get("fair_value") if isinstance(self.config.get("fair_value"), dict) else {}
        self.model_mature = fair_policy.get("default_model_mature") is True
        cohorts = fair_policy.get("live_shadow_cohorts") if isinstance(
            fair_policy.get("live_shadow_cohorts"), list
        ) else []
        hybrid = next((row for row in cohorts if isinstance(row, dict)
                       and row.get("id") == "hybrid_fair"), {})
        self.hybrid_market_weight = min(1.0, max(
            0.0, finite(hybrid.get("market_prior_logit_weight"), 0.35)
        ))
        self.clob_url = clob_url.rstrip("/")
        self.gamma_url = gamma_url.rstrip("/")
        self.source = self.directory / "status.json"
        self.status_path = self.directory / "paper_router_status.json"
        self.state_path = self.directory / "paper_router_state.json"
        self.counterfactual_path = self.directory / "counterfactuals.jsonl"
        # Production uses runs/paper_v7_live, while unit tests often pass an
        # arbitrary temporary directory.  Keep both layouts isolated and put
        # durable evidence beside (never inside) the ephemeral live run.
        family_root = run_root.parent if run_root.name == "paper_v7_live" else run_root
        self.archive_root = family_root / "paper_v7_archives"
        self.durable_directory = family_root / "paper_v7_durable" / "external_fair"
        self.durable_counterfactual_path = self.durable_directory / "counterfactuals.jsonl"
        self.drain_path = run_root / "control" / "CUTOVER_DRAIN"
        allocation = load(run_root / "control" / "allocations" / "manifest.json")
        budgets = allocation.get("budgets") if isinstance(allocation.get("budgets"), dict) else {}
        starting_capital = max(1.0, finite(budgets.get("external"), 4000.0))
        self.state: dict[str, Any] = {
            "model_sha": model_sha, "starting_capital": starting_capital,
            "cash": starting_capital, "orders": 0, "fills": 0,
            "candidates": 0, "counterfactual_fills": 0, "nothing": 0,
            "realized_pnl": 0.0, "counterfactual_realized_pnl": 0.0,
            "peak_equity": starting_capital, "killed": False,
            "attempted_at": {}, "traded_markets": [], "positions": {},
            "book_requests": 0, "book_request_failures": 0,
            "book_parse_failures": 0, "rejection_reasons": {},
            "wait_reasons": {},
            "forecasts": 0, "resolved_forecasts": 0,
            "forecasted_keys": [], "pending_forecasts": {},
            "forecast_settlement_attempted_at": {},
            "last_decision": {},
        }
        prior = load(self.state_path)
        if prior.get("model_sha") == model_sha:
            self.state.update(prior)
        self.compact_durable_evidence()
        self.restore_durable_state()
        self.last_book_error = ""
        self.last_attempt_reason = ""
        self.last_live_market: dict[str, Any] = {}

    def evidence_compatible(self, row: dict[str, Any]) -> bool:
        """Pool only explicitly compatible, permanently SHADOW observations."""
        semantics = str(row.get("evidence_semantics_version") or "")
        return (
            row.get("schema") == "polymarket_v7_external_fair_counterfactual_v1"
            and row.get("paper_only") is True
            and row.get("authenticated_execution") is False
            and row.get("real_order_submission") is False
            and row.get("execution_authority") == "SHADOW_ZERO_AUTHORITY"
            and row.get("model_version") == MODEL_VERSION
            and row.get("policy_sha256") == self.policy_sha256
            # One-time migration for same-policy evidence written immediately
            # before this field existed. Incompatible configurations have a
            # different policy hash and remain excluded.
            and semantics in {"", EVIDENCE_SEMANTICS_VERSION}
        )

    @staticmethod
    def read_counterfactual_records(paths: list[Path]) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        for path in paths:
            try:
                with path.open(encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(row, dict) and row.get("record_id"):
                            records.setdefault(str(row["record_id"]), row)
            except OSError:
                continue
        return records

    def evidence_source_paths(self) -> list[Path]:
        paths = [self.durable_counterfactual_path, self.counterfactual_path]
        if self.archive_root.exists():
            paths.extend(sorted(
                self.archive_root.glob("cutover-*/external_fair/counterfactuals.jsonl")
            ))
        return paths

    def compact_durable_evidence(self) -> None:
        """Deduplicate compatible evidence before old cutover trees are pruned."""
        records = self.read_counterfactual_records(self.evidence_source_paths())
        compatible = [
            row for row in records.values() if self.evidence_compatible(row)
        ]
        compatible.sort(key=lambda row: (
            int(row.get("timestamp_ms") or 0), str(row.get("record_id") or "")
        ))
        self.durable_directory.mkdir(parents=True, exist_ok=True)
        temporary = self.durable_counterfactual_path.with_name(
            self.durable_counterfactual_path.name + f".tmp.{os.getpid()}"
        )
        with temporary.open("w", encoding="utf-8") as handle:
            for row in compatible:
                handle.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.durable_counterfactual_path)

    def durable_records(self) -> dict[str, dict[str, Any]]:
        return {
            record_id: row for record_id, row in self.read_counterfactual_records(
                [self.durable_counterfactual_path, self.counterfactual_path]
            ).items() if self.evidence_compatible(row)
        }

    def restore_durable_state(self) -> None:
        """Restore unresolved forecasts and virtual positions across cutovers."""
        records = self.durable_records()
        forecasts: dict[str, dict[str, Any]] = {}
        forecast_finals: set[str] = set()
        fills: dict[str, dict[str, Any]] = {}
        fill_finals: dict[str, dict[str, Any]] = {}
        markout_horizons: dict[str, set[int]] = {}
        candidate_ids: set[str] = set()
        for row in records.values():
            event_type = str(row.get("event_type") or "")
            forecast_id = str(row.get("forecast_id") or "")
            fill_id = str(row.get("fill_id") or "")
            if event_type == "FORECAST" and forecast_id:
                forecasts.setdefault(forecast_id, row)
            elif event_type == "FORECAST_FINAL" and forecast_id:
                forecast_finals.add(forecast_id)
            elif event_type == "VIRTUAL_FILL" and fill_id:
                fills.setdefault(fill_id, row)
            elif event_type == "VIRTUAL_FINAL" and fill_id:
                fill_finals.setdefault(fill_id, row)
            elif event_type == "VIRTUAL_MARKOUT" and fill_id:
                for key in (row.get("markouts") or {}):
                    try:
                        markout_horizons.setdefault(fill_id, set()).add(
                            int(str(key).removesuffix("s"))
                        )
                    except ValueError:
                        continue
            if event_type == "CANDIDATE" and row.get("counterfactual_id"):
                candidate_ids.add(str(row["counterfactual_id"]))

        seen_keys = {
            f"{row.get('market_id')}:{int(row.get('tte_bucket_seconds') or 0)}"
            for row in forecasts.values() if row.get("market_id")
        }
        pending: dict[str, dict[str, Any]] = {}
        for forecast_id, row in forecasts.items():
            if forecast_id in forecast_finals:
                continue
            required = ("market_id", "yes_token", "no_token", "resolution_due_ms")
            if not all(row.get(key) not in {None, ""} for key in required):
                continue
            pending[forecast_id] = {
                key: row.get(key) for key in (
                    "counterfactual_id", "forecast_id", "market_id", "event_id",
                    "rules_hash", "reference_version", "tte_bucket_seconds",
                    "observed_tte_seconds", "model_yes", "market_yes",
                    "external_only_yes", "hybrid_yes", "external_only_model_id",
                    "hybrid_model_id", "registered_challenger_yes",
                    "registered_challenger_model_id",
                    "registered_challenger_model_hash", "lower", "upper", "oracle_value",
                    "external_venue_count", "fair_calculated_monotonic_ns",
                    "fair_valid_until_monotonic_ns", "yes_best_bid", "yes_best_ask",
                    "no_best_bid", "no_best_ask", "market_mid_source", "decision",
                    "yes_token", "no_token", "observed_ms", "resolution_due_ms",
                )
            }

        restored_positions: dict[str, dict[str, Any]] = {}
        for fill_id, row in fills.items():
            if fill_id in fill_finals:
                continue
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            try:
                shares = float(row.get("filled_size"))
                price = float(row.get("fill_price"))
                fee = float(row.get("fee") or 0.0)
            except (TypeError, ValueError):
                continue
            position_id = str(row.get("position_id") or "")
            if not position_id or shares <= 0.0 or not 0.0 < price < 1.0:
                continue
            restored_positions[position_id] = {
                "position_id": position_id,
                "counterfactual_id": str(row.get("counterfactual_id") or ""),
                "fill_id": fill_id,
                "order_id": str(row.get("counterfactual_id") or ""),
                "market_id": str(row.get("market_id") or ""),
                "event_id": str(row.get("event_id") or ""),
                "token_id": str(row.get("token_id") or ""),
                "outcome": str(metadata.get("outcome") or ""),
                "shares": shares, "entry_price": price,
                "entry_fee": fee, "entry_cost": shares * price,
                "executable_value": 0.0,
                "opened_ms": int(row.get("receive_ts_ms") or row.get("timestamp_ms") or 0),
                "fee_schedule": row.get("fee_schedule") if isinstance(
                    row.get("fee_schedule"), dict) else {
                        "rate": float(row.get("fee_rate") or 0.0),
                        "exponent": 1, "takerOnly": True,
                    },
                "markouts": sorted(markout_horizons.get(fill_id, set())),
                "settled": False,
                "model_yes": finite(metadata.get("fair_yes")),
                "market_yes": finite(metadata.get("arrival_pm_mid")),
                "market_mid_source": "LIVE_COMPLEMENT_CONSISTENT_CLOB_BATCH",
            }

        self.state["forecasts"] = len(forecasts)
        self.state["resolved_forecasts"] = len(forecast_finals)
        self.state["forecasted_keys"] = sorted(seen_keys)[-10_000:]
        self.state["pending_forecasts"] = pending
        self.state["counterfactual_fills"] = len(fills)
        self.state["candidates"] = len(candidate_ids)
        self.state["counterfactual_realized_pnl"] = sum(
            finite(row.get("counterfactual_pnl"), 0.0)
            for row in fill_finals.values()
        )
        self.state["traded_markets"] = sorted({
            str(row.get("market_id") or "") for row in fills.values()
            if row.get("market_id")
        })
        current_positions = self.state.get("positions") if isinstance(
            self.state.get("positions"), dict) else {}
        current_positions = {
            position_id: position for position_id, position in current_positions.items()
            if not isinstance(position, dict)
            or str(position.get("fill_id") or "") not in fill_finals
        }
        current_positions.update(restored_positions)
        self.state["positions"] = current_positions

    def reject(self, reason: str) -> None:
        reasons = self.state.setdefault("rejection_reasons", {})
        reasons[reason] = int(reasons.get(reason) or 0) + 1

    def wait(self, reason: str) -> None:
        reasons = self.state.setdefault("wait_reasons", {})
        reasons[reason] = int(reasons.get(reason) or 0) + 1

    def maturity_diagnostics(self) -> dict[str, Any]:
        """Fail-closed settlement-cluster gate; never grants authority automatically."""
        records = self.durable_records()
        # A process may crash after appending a final but before publishing its
        # state. Deduplicate by the causal forecast identity so retries cannot
        # overweight one settlement cluster.
        forecast_finals: dict[str, dict[str, Any]] = {}
        for row in records.values():
            forecast_id = str(row.get("forecast_id") or "")
            if row.get("event_type") == "FORECAST_FINAL" and forecast_id:
                forecast_finals.setdefault(forecast_id, row)
        forecasts = list(forecast_finals.values())
        by_market: dict[str, list[dict[str, Any]]] = {}
        for row in forecasts:
            by_market.setdefault(str(row.get("market_id") or "UNKNOWN"), []).append(row)
        cluster_model_brier: list[float] = []
        cluster_market_brier: list[float] = []
        cluster_delta: list[float] = []
        cluster_predictions: list[float] = []
        cluster_actuals: list[float] = []
        cluster_lowers: list[float] = []
        cluster_uppers: list[float] = []
        for rows in by_market.values():
            model_losses = [finite(row.get("model_brier"), math.nan) for row in rows]
            market_losses = [finite(row.get("market_brier"), math.nan) for row in rows]
            pairs = [(left, right) for left, right in zip(model_losses, market_losses)
                     if math.isfinite(left) and math.isfinite(right)]
            if pairs:
                model_loss = sum(left for left, _ in pairs) / len(pairs)
                market_loss = sum(right for _, right in pairs) / len(pairs)
                cluster_model_brier.append(model_loss)
                cluster_market_brier.append(market_loss)
                cluster_delta.append(model_loss - market_loss)
            probabilities = [finite(row.get("model_yes"), math.nan) for row in rows]
            actual = finite(rows[0].get("actual_yes"), math.nan)
            probabilities = [value for value in probabilities if math.isfinite(value)]
            if probabilities and math.isfinite(actual):
                cluster_predictions.append(sum(probabilities) / len(probabilities))
                cluster_actuals.append(actual)
                valid_bands = [
                    (finite(row.get("lower"), math.nan),
                     finite(row.get("upper"), math.nan))
                    for row in rows
                ]
                valid_bands = [
                    (lower, upper) for lower, upper in valid_bands
                    if math.isfinite(lower) and math.isfinite(upper)
                    and 0.0 <= lower <= upper <= 1.0
                ]
                if valid_bands:
                    cluster_lowers.append(statistics.fmean(
                        lower for lower, _ in valid_bands))
                    cluster_uppers.append(statistics.fmean(
                        upper for _, upper in valid_bands))
                else:
                    # Keep vector alignment; invalid bands fail diagnostics.
                    cluster_lowers.append(math.nan)
                    cluster_uppers.append(math.nan)
        delta_mean = sum(cluster_delta) / len(cluster_delta) if cluster_delta else None
        delta_upper = None
        if len(cluster_delta) >= 2 and delta_mean is not None:
            delta_upper = delta_mean + 1.96 * statistics.stdev(cluster_delta) / math.sqrt(
                len(cluster_delta))
        calibration_error = expected_calibration_error(
            cluster_predictions, cluster_actuals)
        calibration_intercept, slope = logistic_calibration_line(
            cluster_predictions, cluster_actuals)
        interval_diagnostics = probability_interval_bin_diagnostics(
            cluster_predictions, cluster_actuals, cluster_lowers, cluster_uppers,
            bins=int((self.config.get("promotion") or {}).get(
                "probability_interval_bins", 10)),
            minimum_bin_size=int((self.config.get("promotion") or {}).get(
                "minimum_probability_interval_bin_size", 3)),
        )
        challenger_pointer = load(
            self.directory / "model_registry" / "fair_value_challenger.json")
        current_challenger_hash = str(challenger_pointer.get("model_hash") or "")
        challenger_by_market: dict[str, list[dict[str, Any]]] = {}
        for row in forecasts:
            probability = finite(row.get("registered_challenger_yes"), math.nan)
            model_hash = str(row.get("registered_challenger_model_hash") or "")
            market_id = str(row.get("market_id") or "")
            if (
                math.isfinite(probability) and len(current_challenger_hash) == 64
                and model_hash == current_challenger_hash and market_id
            ):
                challenger_by_market.setdefault(market_id, []).append(row)
        challenger_model_losses: list[float] = []
        challenger_market_losses: list[float] = []
        for rows in challenger_by_market.values():
            challenger_pairs = [
                (finite(row.get("registered_challenger_brier"), math.nan),
                 finite(row.get("market_brier"), math.nan))
                for row in rows
            ]
            challenger_pairs = [pair for pair in challenger_pairs
                                if all(math.isfinite(value) for value in pair)]
            if challenger_pairs:
                challenger_model_losses.append(statistics.fmean(
                    value[0] for value in challenger_pairs))
                challenger_market_losses.append(statistics.fmean(
                    value[1] for value in challenger_pairs))
        challenger_delta = (
            statistics.fmean(challenger_model_losses)
            - statistics.fmean(challenger_market_losses)
            if challenger_model_losses and challenger_market_losses else None
        )
        fills = {
            str(row.get("fill_id")): row for row in records.values()
            if row.get("event_type") == "VIRTUAL_FILL" and row.get("fill_id")
        }
        finals_by_fill: dict[str, dict[str, Any]] = {}
        for row in records.values():
            fill_id = str(row.get("fill_id") or "")
            if row.get("event_type") == "VIRTUAL_FINAL" and fill_id:
                finals_by_fill.setdefault(fill_id, row)
        finals = list(finals_by_fill.values())
        stressed_2x = 0.0
        matched_finals = 0
        for final in finals:
            pnl = finite(final.get("counterfactual_pnl"), math.nan)
            fill = fills.get(str(final.get("fill_id") or ""))
            if fill is None or not math.isfinite(pnl):
                continue
            stressed_2x += pnl - max(0.0, finite(fill.get("fee"), 0.0)) \
                - max(0.0, finite(fill.get("slippage"), 0.0))
            matched_finals += 1
        promotion = self.config.get("promotion") if isinstance(
            self.config.get("promotion"), dict) else {}
        minimum = int(promotion.get("minimum_forward_shadow_contracts") or 50)
        slope_range = promotion.get("calibration_slope_range") or [0.75, 1.25]
        interval_consistency = interval_diagnostics["consistency_rate"]
        interval_bin_count = int(interval_diagnostics["eligible_bin_count"])
        interval_width = interval_diagnostics["mean_probability_band_width"]
        minimum_interval_bins = int(
            promotion.get("minimum_probability_interval_bins") or 5)
        minimum_interval_consistency = float(
            promotion.get("minimum_probability_interval_bin_consistency") or 0.80)
        maximum_interval_width = float(
            promotion.get("maximum_mean_probability_interval_width") or 0.20)
        reasons: list[str] = []
        if len(by_market) < minimum:
            reasons.append("INSUFFICIENT_INDEPENDENT_SETTLEMENT_MARKETS")
        if delta_upper is None or delta_upper >= 0.0:
            reasons.append("MODEL_NOT_CLUSTER_ROBUST_BETTER_THAN_PM")
        if calibration_error is None or calibration_error > float(promotion.get("maximum_ece", 0.05)):
            reasons.append("CALIBRATION_ERROR_GATE")
        if slope is None or not float(slope_range[0]) <= slope <= float(slope_range[1]):
            reasons.append("CALIBRATION_SLOPE_GATE")
        if (
            interval_bin_count < minimum_interval_bins
            or interval_consistency is None
            or interval_consistency < minimum_interval_consistency
            or interval_width is None
            or interval_width > maximum_interval_width
        ):
            reasons.append("PROBABILITY_INTERVAL_CALIBRATION_GATE")
        final_markets = {str(row.get("market_id") or "") for row in finals}
        final_markets.discard("")
        if len(final_markets) < minimum or matched_finals < minimum or stressed_2x <= 0.0:
            reasons.append("POSITIVE_2X_COST_STRESS_GATE")
        return {
            "eligible_for_manual_paper_promotion": not reasons,
            "automatic_promotion": False,
            "independent_settlement_markets": len(by_market),
            "minimum_independent_settlement_markets": minimum,
            "forecast_rows": len(forecasts),
            "model_brier_cluster_equal_weighted": (
                sum(cluster_model_brier) / len(cluster_model_brier)
                if cluster_model_brier else None),
            "market_brier_cluster_equal_weighted": (
                sum(cluster_market_brier) / len(cluster_market_brier)
                if cluster_market_brier else None),
            "model_minus_market_brier_mean": delta_mean,
            "model_minus_market_brier_ci95_upper": delta_upper,
            "calibration_error": calibration_error,
            "calibration_error_definition": "10-bin ECE, settlement clusters equal weighted",
            "calibration_intercept": calibration_intercept,
            "calibration_slope": slope,
            "calibration_slope_definition": (
                "logistic MLE: outcome ~ intercept + slope*logit(prediction), "
                "one equal-weighted observation per settlement market"),
            "probability_interval_diagnostics": interval_diagnostics,
            "interval_coverage": None,
            "interval_coverage_definition": (
                "DEPRECATED_INVALID_FOR_BERNOULLI_REALIZATIONS; use "
                "probability_interval_diagnostics"),
            "virtual_final_markets": len(final_markets),
            "virtual_2x_cost_stress_pnl": stressed_2x,
            "registered_challenger_forward": {
                "state": (
                    "FORWARD_EVIDENCE_ACCUMULATING" if challenger_by_market
                    else "AWAITING_FORWARD_SETTLEMENTS"
                ),
                "independent_settlement_markets": len(challenger_by_market),
                "model_hash": (
                    current_challenger_hash if len(current_challenger_hash) == 64
                    else None
                ),
                "model_brier_cluster_equal_weighted": (
                    statistics.fmean(challenger_model_losses)
                    if challenger_model_losses else None
                ),
                "market_brier_cluster_equal_weighted": (
                    statistics.fmean(challenger_market_losses)
                    if challenger_market_losses else None
                ),
                "model_minus_market_brier_mean": challenger_delta,
                "execution_authority": "SHADOW_ZERO_AUTHORITY",
            },
            "blocking_reasons": reasons,
        }

    def emit_counterfactual(self, event_type: str, **values: Any) -> None:
        timestamp_ms = now_ms()
        markout_keys = sorted(
            str(key) for key in (
                values.get("markouts") if isinstance(values.get("markouts"), dict) else {}
            )
        )
        record = {
            "schema": "polymarket_v7_external_fair_counterfactual_v1",
            "record_id": stable_id(
                self.sha, event_type, values.get("counterfactual_id"),
                values.get("forecast_id"), values.get("position_id"),
                values.get("reason"), markout_keys,
            ),
            "event_type": event_type,
            "timestamp_ms": timestamp_ms,
            "model_sha": self.sha,
            "model_version": MODEL_VERSION,
            "evidence_semantics_version": EVIDENCE_SEMANTICS_VERSION,
            "policy_sha256": self.policy_sha256,
            "execution_authority": "SHADOW_ZERO_AUTHORITY",
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            **values,
        }
        payload = (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode()
        for path in (self.counterfactual_path, self.durable_counterfactual_path):
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def emit_canonical_shadow(self, event: LedgerEvent) -> None:
        """Route shadow evidence through the sole canonical writer transport."""
        metadata = dict(event.metadata)
        metadata.update({
            "counterfactual": True,
            "economic_authority": "SHADOW_COUNTERFACTUAL",
            "excluded_from_portfolio_equity": True,
        })
        spool_event(self.root, LedgerEvent(**{
            **event.to_dict(), "metadata": metadata,
        }))

    def fetch_book(self, token_id: str) -> Book | None:
        try:
            raw = request_json(
                f"{self.clob_url}/book?token_id={urllib.parse.quote(token_id)}", timeout=4
            )
        except Exception:
            return None
        return parse_book(raw, now_ms())

    def books_for(self, status: dict[str, Any]) -> dict[str, Book]:
        market = status.get("market") if isinstance(status.get("market"), dict) else {}
        output: dict[str, Book] = {}
        tokens = [token for token in (
            str(market.get("yes_token") or ""), str(market.get("no_token") or "")
        ) if token]
        self.state["book_requests"] = int(self.state.get("book_requests") or 0) + 1
        self.last_book_error = ""
        try:
            rows = request_json(
                f"{self.clob_url}/books", [{"token_id": token} for token in tokens], timeout=4
            )
        except Exception as exc:
            self.state["book_request_failures"] = int(self.state.get("book_request_failures") or 0) + 1
            self.last_book_error = f"CLOB_BOOK_REQUEST_{type(exc).__name__.upper()}"
            rows = []
        received = now_ms()
        for raw in rows if isinstance(rows, list) else []:
            book = parse_book(raw, received)
            if book is not None and book.token_id in tokens:
                output[book.token_id] = book
        if tokens and len(output) != len(tokens) and not self.last_book_error:
            self.state["book_parse_failures"] = int(self.state.get("book_parse_failures") or 0) + 1
            self.last_book_error = "CLOB_BOOK_SNAPSHOT_INCOMPLETE"
        return output

    def record_forecast(self, status: dict[str, Any], books: dict[str, Book]) -> bool:
        """Persist one trade-independent forecast near each canonical TTE bucket."""
        fair = status.get("fair") if isinstance(status.get("fair"), dict) else {}
        market = status.get("market") if isinstance(status.get("market"), dict) else {}
        contract = status.get("contract") if isinstance(status.get("contract"), dict) else {}
        reference = status.get("settlement_reference") if isinstance(status.get("settlement_reference"), dict) else {}
        oracle = status.get("oracle") if isinstance(status.get("oracle"), dict) else {}
        external = status.get("external") if isinstance(status.get("external"), dict) else {}
        fair_models = status.get("fair_models") if isinstance(
            status.get("fair_models"), dict) else {}
        challenger = fair_models.get("registered_challenger") if isinstance(
            fair_models.get("registered_challenger"), dict) else {}
        tte = finite(fair.get("tte_seconds"))
        model_yes = finite(fair.get("yes"))
        market_yes = live_market_yes(books, market)
        hybrid_yes = (
            hybrid_probability(model_yes, market_yes, self.hybrid_market_weight)
            if math.isfinite(model_yes) and market_yes is not None else math.nan
        )
        challenger_yes = finite(challenger.get("yes"), math.nan)
        challenger_hash = str(challenger.get("probability_model_hash") or "")
        challenger_applied = bool(
            challenger.get("valid") is True
            and challenger.get("explicit_registry_model_applied") is True
            and math.isfinite(challenger_yes)
            and 0.0 <= challenger_yes <= 1.0
            and len(challenger_hash) == 64
        )
        market_id = str(market.get("market_id") or "")
        if not (
            fair.get("valid") is True and reference.get("valid") is True
            and market_id and tte is not None and tte >= 0.0
            and model_yes is not None and 0.0 <= model_yes <= 1.0
            and market_yes is not None and 0.0 <= market_yes <= 1.0
        ):
            return False
        bucket = min(FORECAST_TTE_BUCKETS, key=lambda value: (abs(value - tte), -value))
        if abs(bucket - tte) > FORECAST_BUCKET_TOLERANCE_SECONDS:
            return False
        key = f"{market_id}:{bucket}"
        seen = self.state.get("forecasted_keys") if isinstance(self.state.get("forecasted_keys"), list) else []
        if key in set(str(value) for value in seen):
            return False
        forecast_id = f"external-forecast-{stable_id(self.sha, market_id, bucket)}"
        yes_token, no_token = str(market.get("yes_token") or ""), str(market.get("no_token") or "")
        yes_book, no_book = books.get(yes_token), books.get(no_token)
        observed_ms = now_ms()
        resolution_due_ms = observed_ms + int(tte * 1000.0)
        values = {
            "counterfactual_id": forecast_id, "forecast_id": forecast_id,
            "market_id": market_id, "event_id": str(market.get("event_id") or ""),
            "rules_hash": str(contract.get("rules_hash") or ""),
            "reference_version": int(reference.get("version") or 0),
            "tte_bucket_seconds": bucket, "observed_tte_seconds": tte,
            "model_yes": model_yes, "market_yes": market_yes,
            "external_only_yes": model_yes,
            "hybrid_yes": hybrid_yes if math.isfinite(hybrid_yes) else None,
            "external_only_model_id": "external_only_fair",
            "hybrid_model_id": "hybrid_fair",
            "registered_challenger_yes": challenger_yes if challenger_applied else None,
            "registered_challenger_model_id": (
                str(challenger.get("probability_model_id") or "")
                if challenger_applied else ""
            ),
            "registered_challenger_model_hash": (
                challenger_hash if challenger_applied else ""
            ),
            "lower": finite(fair.get("lower")), "upper": finite(fair.get("upper")),
            "oracle_value": finite(oracle.get("value")),
            "external_venue_count": int(external.get("fresh_venue_count") or 0),
            "fair_calculated_monotonic_ns": int(fair.get("calculated_monotonic_ns") or 0),
            "fair_valid_until_monotonic_ns": int(fair.get("valid_until_monotonic_ns") or 0),
            "yes_best_bid": yes_book.bids[0][0] if yes_book and yes_book.bids else None,
            "yes_best_ask": yes_book.asks[0][0] if yes_book and yes_book.asks else None,
            "no_best_bid": no_book.bids[0][0] if no_book and no_book.bids else None,
            "no_best_ask": no_book.asks[0][0] if no_book and no_book.asks else None,
            "market_mid_source": "LIVE_COMPLEMENT_CONSISTENT_CLOB_BATCH",
            "decision": "SHADOW_FORECAST_ONLY",
            # Persist enough settlement identity to resume a pending forecast
            # after the ephemeral live run is archived during a cutover.
            "yes_token": yes_token, "no_token": no_token,
            "observed_ms": observed_ms, "resolution_due_ms": resolution_due_ms,
        }
        self.emit_counterfactual("FORECAST", **values)
        seen.append(key)
        self.state["forecasted_keys"] = seen[-10_000:]
        self.state["forecasts"] = int(self.state.get("forecasts") or 0) + 1
        self.state.setdefault("pending_forecasts", {})[forecast_id] = {
            **values,
        }
        return True

    @staticmethod
    def forecast_scores(probability: float, actual: float) -> tuple[float, float]:
        clipped = min(1.0 - 1e-12, max(1e-12, probability))
        brier = (clipped - actual) ** 2
        log_loss = -(actual * math.log(clipped) + (1.0 - actual) * math.log(1.0 - clipped))
        return brier, log_loss

    def observe_forecasts(self) -> None:
        current_ms = now_ms()
        pending = self.state.get("pending_forecasts") if isinstance(self.state.get("pending_forecasts"), dict) else {}
        by_market: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for forecast_id, forecast in pending.items():
            if current_ms < int(forecast.get("resolution_due_ms") or 0) + 5000:
                continue
            by_market.setdefault(str(forecast.get("market_id") or ""), []).append((forecast_id, forecast))
        attempted = self.state.setdefault("forecast_settlement_attempted_at", {})
        for market_id, forecasts in by_market.items():
            if not market_id or current_ms - int(attempted.get(market_id) or 0) < 10_000:
                continue
            attempted[market_id] = current_ms
            try:
                raw = request_json(
                    f"{self.gamma_url}/markets/{urllib.parse.quote(market_id)}", timeout=4
                )
            except Exception:
                continue
            if not isinstance(raw, dict) or raw.get("closed") is not True:
                continue
            tokens = [str(value) for value in parse_array(raw.get("clobTokenIds"))]
            prices = [finite(value) for value in parse_array(raw.get("outcomePrices"))]
            winning_index = next((index for index, price in enumerate(prices)
                                  if math.isfinite(price) and price >= 1.0 - 1e-9), -1)
            if winning_index < 0 or winning_index >= len(tokens):
                continue
            winning_token = tokens[winning_index]
            for forecast_id, forecast in forecasts:
                if winning_token not in {
                    str(forecast.get("yes_token") or ""),
                    str(forecast.get("no_token") or ""),
                }:
                    continue
                actual_yes = 1.0 if winning_token == str(forecast.get("yes_token") or "") else 0.0
                model_brier, model_log_loss = self.forecast_scores(float(forecast["model_yes"]), actual_yes)
                hybrid_yes = finite(forecast.get("hybrid_yes"))
                if math.isfinite(hybrid_yes):
                    hybrid_brier, hybrid_log_loss = self.forecast_scores(hybrid_yes, actual_yes)
                else:
                    hybrid_brier = hybrid_log_loss = None
                market_brier, market_log_loss = self.forecast_scores(float(forecast["market_yes"]), actual_yes)
                challenger_yes = finite(forecast.get("registered_challenger_yes"))
                if math.isfinite(challenger_yes):
                    challenger_brier, challenger_log_loss = self.forecast_scores(
                        challenger_yes, actual_yes)
                else:
                    challenger_brier = challenger_log_loss = None
                self.emit_counterfactual(
                    "FORECAST_FINAL", counterfactual_id=forecast_id, forecast_id=forecast_id,
                    market_id=market_id, tte_bucket_seconds=forecast["tte_bucket_seconds"],
                    model_yes=forecast["model_yes"], market_yes=forecast["market_yes"],
                    actual_yes=actual_yes, winning_token_id=winning_token,
                    model_brier=model_brier, market_brier=market_brier,
                    model_log_loss=model_log_loss, market_log_loss=market_log_loss,
                    external_only_brier=model_brier,
                    external_only_log_loss=model_log_loss,
                    hybrid_yes=hybrid_yes if math.isfinite(hybrid_yes) else None,
                    hybrid_brier=hybrid_brier, hybrid_log_loss=hybrid_log_loss,
                    lower=forecast.get("lower"), upper=forecast.get("upper"),
                    observed_tte_seconds=forecast.get("observed_tte_seconds"),
                    external_only_model_id=forecast.get("external_only_model_id"),
                    hybrid_model_id=forecast.get("hybrid_model_id"),
                    registered_challenger_yes=(
                        challenger_yes if math.isfinite(challenger_yes) else None),
                    registered_challenger_brier=challenger_brier,
                    registered_challenger_log_loss=challenger_log_loss,
                    registered_challenger_model_id=forecast.get(
                        "registered_challenger_model_id"),
                    registered_challenger_model_hash=forecast.get(
                        "registered_challenger_model_hash"),
                )
                pending.pop(forecast_id, None)
                self.state["resolved_forecasts"] = int(self.state.get("resolved_forecasts") or 0) + 1

    def order_size(self, row: dict[str, Any]) -> float:
        book: Book = row["book"]
        ask, visible = book.asks[0]
        depth_fraction = min(
            float(self.policy.get("max_depth_fraction", 0.5)),
            float(self.policy.get("depth_survival_fraction", 0.75)),
        )
        starting_capital = float(self.state.get("starting_capital") or 0.0)
        cash = float(self.state.get("cash") or 0.0)
        if self.model_mature:
            capital_ceiling = starting_capital * float(
                self.policy.get("max_market_capital_fraction", 0.02)
            )
            probability = max(1e-6, min(1.0 - 1e-6, float(row["robust_probability"])))
            kelly = max(0.0, float(row["robust_ev"])) / (probability * (1.0 - probability))
            kelly_notional = cash * float(self.policy.get("fractional_kelly", 0.1)) * kelly
            available_notional = min(capital_ceiling, kelly_notional, cash)
        else:
            # Kelly sizing is not mathematically defensible before probability
            # calibration is mature. Shadow observations use a fixed virtual
            # notional so their economics stay comparable across contracts.
            capital_ceiling = starting_capital * float(
                self.policy.get("immature_exploration_capital_fraction", 0.0025)
            )
            available_notional = min(capital_ceiling, cash)
        size = min(visible * max(0.0, depth_fraction), available_notional / max(ask + row["fee_per_share"], 1e-9))
        size = math.floor(size * 100.0) / 100.0
        return size if size + 1e-9 >= book.min_order_size else 0.0

    def common(self, status: dict[str, Any], row: dict[str, Any], order_id: str, size: float) -> dict[str, Any]:
        market, fair, contract, reference = (
            status.get("market") or {}, status.get("fair") or {}, status.get("contract") or {},
            status.get("settlement_reference") or {},
        )
        book: Book = row["book"]
        decision = now_ms()
        market_id = str(market.get("market_id") or "")
        position_id = f"external-position-{market_id}-{row['outcome']}"
        return dict(
            strategy=STRATEGY, model_sha=self.sha, model_version=MODEL_VERSION,
            candidate_id=order_id, order_id=order_id, position_id=position_id,
            market_id=market_id,
            event_id=str(market.get("event_id") or ""), token_id=row["token_id"],
            decision_ts_ms=decision, exchange_ts_ms=book.exchange_ts_ms,
            receive_ts_ms=book.receive_ts_ms, book_snapshot_id=book.snapshot_id,
            side="BUY", bid=book.bids[0][0] if book.bids else None, ask=book.asks[0][0],
            bid_depth=sum(size for _, size in book.bids), ask_depth=sum(size for _, size in book.asks),
            limit_price=book.asks[0][0], predicted_alpha=row["robust_ev"],
            predicted_fill_probability=1.0, expected_ev=row["robust_ev"] * size,
            intended_action="TAKE", intended_size=size,
            metadata={
                "authority": "SHADOW_ZERO_AUTHORITY", "virtual_tif": "FAK",
                "outcome": row["outcome"], "execution_side": "BUY",
                "fair_yes": fair.get("yes"), "fair_lower": fair.get("lower"),
                "fair_upper": fair.get("upper"), "pm_mid": row["market_yes"],
                "pm_mid_source": "LIVE_COMPLEMENT_CONSISTENT_CLOB_BATCH",
                "gamma_discovery_mid_diagnostic": fair.get(
                    "gamma_discovery_mid_diagnostic"
                ),
                "model_market_disagreement": abs(
                    float(fair.get("yes")) - float(row["market_yes"])
                ),
                "maximum_model_market_disagreement": self.policy.get(
                    "maximum_model_market_disagreement"
                ),
                "contract_rules_hash": contract.get("rules_hash"),
                "reference_version": reference.get("version"), "expected_fee_per_share": row["fee_per_share"],
                "expected_execution_risk": row["execution_risk"], "economic_maturity": "MORE_EVIDENCE_REQUIRED",
                "tte_seconds": row["tte_seconds"], "robust_probability": row["robust_probability"],
                "robust_ev_per_share": row["robust_ev"],
                "tte_bucket_id": row.get("tte_bucket_id", "legacy_entry_window"),
                "model_family": STRATEGY, "horizon_seconds": 300,
            },
        )

    def attempt(self, status: dict[str, Any], row: dict[str, Any]) -> bool:
        self.last_attempt_reason = ""
        if self.drain_path.exists():
            self.last_attempt_reason = "CUTOVER_DRAIN"
            return False
        if self.state.get("killed") or (self.root / "control" / "KILL").exists():
            self.last_attempt_reason = "GLOBAL_OR_SLEEVE_KILLED"
            return False
        market = status.get("market") if isinstance(status.get("market"), dict) else {}
        market_id = str(market.get("market_id") or "")
        if not market_id:
            self.last_attempt_reason = "MARKET_ID_MISSING"
            return False
        if market_id in set(self.state.get("traded_markets") or []):
            self.last_attempt_reason = "WAITING_FOR_NEXT_CONTRACT_HANDOFF"
            return False
        key = f"{market_id}:{row['outcome']}"
        current_ms = now_ms()
        if current_ms - int((self.state.get("attempted_at") or {}).get(key) or 0) < 5000:
            self.last_attempt_reason = "ATTEMPT_COOLDOWN"
            return False
        self.state.setdefault("attempted_at", {})[key] = current_ms
        size = self.order_size(row)
        if size <= 0.0:
            self.last_attempt_reason = "BELOW_MINIMUM_EXECUTABLE_SIZE"
            return False
        counterfactual_id = f"external-shadow-{stable_id(self.sha, market_id, row['outcome'], current_ms)}"
        common = self.common(status, row, counterfactual_id, size)
        self.emit_counterfactual("CANDIDATE", counterfactual_id=counterfactual_id, **common)
        self.emit_canonical_shadow(LedgerEvent(
            event_type="CANDIDATE", **common,
        ))
        self.state["candidates"] = int(self.state.get("candidates") or 0) + 1
        time.sleep(0.1)

        arrival_status = load(self.source)
        arrival_books = self.books_for(arrival_status)
        rows = robust_candidates(arrival_status, arrival_books, self.policy)
        arrival = next((candidate for candidate in rows if candidate["token_id"] == row["token_id"]), None)
        if arrival is None:
            self.emit_counterfactual(
                "REJECTED", counterfactual_id=counterfactual_id,
                reason="ARRIVAL_REVALIDATION_FAILED", market_id=market_id,
                event_id=str(market.get("event_id") or ""), token_id=row["token_id"], side="BUY",
            )
            self.last_attempt_reason = "ARRIVAL_REVALIDATION_FAILED"
            return False
        arrival_book: Book = arrival["book"]
        ask, visible = arrival_book.asks[0]
        if visible + 1e-9 < size or ask > float(common["limit_price"]) + 1e-12:
            self.emit_counterfactual(
                "REJECTED", counterfactual_id=counterfactual_id,
                reason="FAK_VISIBLE_DEPTH_OR_LIMIT", market_id=market_id,
                event_id=str(market.get("event_id") or ""), token_id=row["token_id"], side="BUY",
            )
            self.last_attempt_reason = "FAK_VISIBLE_DEPTH_OR_LIMIT"
            return False
        schedule = (arrival_status.get("market") or {}).get("fee_schedule") or {}
        fee_share = fee_per_share(ask, schedule)
        total_fee, cost = size * fee_share, size * ask
        executable_value = executable_sell_value(arrival_book, size, schedule)
        robust_ev = float(arrival["robust_probability"]) * size - cost - total_fee - size * float(arrival["execution_risk"])
        if robust_ev <= 0.0 or cost + total_fee > float(self.state.get("starting_capital") or 0.0):
            self.emit_counterfactual(
                "REJECTED", counterfactual_id=counterfactual_id,
                reason="ARRIVAL_EV_OR_VIRTUAL_CAPITAL", market_id=market_id,
                event_id=str(market.get("event_id") or ""), token_id=row["token_id"], side="BUY",
            )
            self.last_attempt_reason = "ARRIVAL_EV_OR_CAPITAL"
            return False
        fill_id = f"external-shadow-fill-{stable_id(counterfactual_id, arrival_book.exchange_ts_ms, arrival_book.receive_ts_ms)}"
        position_id = str(common["position_id"])
        order_id = str(common["order_id"])
        arrival_decision_ms = max(now_ms(), arrival_book.receive_ts_ms)
        arrival_metadata = {
            **common["metadata"], "robust_net_ev": robust_ev,
            "arrival_snapshot_id": arrival_book.snapshot_id,
            "arrival_tte_seconds": arrival["tte_seconds"],
            "arrival_robust_probability": arrival["robust_probability"],
            "arrival_robust_ev_per_share": arrival["robust_ev"],
            "arrival_pm_mid": arrival["market_yes"],
            "arrival_model_market_disagreement": abs(
                float((arrival_status.get("fair") or {}).get("yes"))
                - float(arrival["market_yes"])
            ),
        }
        self.emit_canonical_shadow(LedgerEvent(
            event_type="ORDER_SUBMITTED", strategy=STRATEGY, model_sha=self.sha,
            model_version=MODEL_VERSION, candidate_id=counterfactual_id,
            order_id=order_id, position_id=position_id, market_id=market_id,
            event_id=str(market.get("event_id") or ""), token_id=row["token_id"], side="BUY",
            exchange_ts_ms=arrival_book.exchange_ts_ms,
            receive_ts_ms=arrival_book.receive_ts_ms, decision_ts_ms=arrival_decision_ms,
            book_snapshot_id=arrival_book.snapshot_id, limit_price=float(common["limit_price"]),
            intended_action="VIRTUAL_FAK", intended_size=size, order_state="SUBMITTED_SHADOW",
            predicted_alpha=arrival["robust_ev"], expected_ev=robust_ev,
            metadata=arrival_metadata,
        ))
        self.emit_canonical_shadow(LedgerEvent(
            event_type="FILL", strategy=STRATEGY, model_sha=self.sha,
            model_version=MODEL_VERSION, candidate_id=counterfactual_id,
            order_id=order_id, position_id=position_id, fill_id=fill_id,
            market_id=market_id, event_id=str(market.get("event_id") or ""),
            token_id=row["token_id"], side="BUY",
            exchange_ts_ms=arrival_book.exchange_ts_ms,
            receive_ts_ms=arrival_book.receive_ts_ms, fill_price=ask, filled_size=size,
            complete=True, fee=total_fee, fee_rate=float(schedule.get("rate") or 0.0),
            fee_source="GAMMA_AUTHORITATIVE_FEE_SCHEDULE",
            slippage=max(0.0, ask - float(common["ask"])) * size,
            metadata=arrival_metadata,
        ))
        self.emit_counterfactual(
            "VIRTUAL_FILL", counterfactual_id=counterfactual_id,
            strategy=STRATEGY, model_version=MODEL_VERSION,
            fill_id=fill_id, position_id=position_id, market_id=market_id,
            event_id=str(market.get("event_id") or ""), token_id=row["token_id"], side="BUY",
            exchange_ts_ms=arrival_book.exchange_ts_ms, receive_ts_ms=arrival_book.receive_ts_ms,
            fill_price=ask, filled_size=size, fee=total_fee,
            fee_rate=float(schedule.get("rate") or 0.0), fee_source="GAMMA_AUTHORITATIVE_FEE_SCHEDULE",
            fee_schedule=schedule,
            slippage=max(0.0, ask - float(common["ask"])) * size,
            metadata={
                **common["metadata"], "robust_net_ev": robust_ev,
                "arrival_snapshot_id": arrival_book.snapshot_id,
                "arrival_tte_seconds": arrival["tte_seconds"],
                "arrival_robust_probability": arrival["robust_probability"],
                "arrival_robust_ev_per_share": arrival["robust_ev"],
                "arrival_pm_mid": arrival["market_yes"],
                "arrival_model_market_disagreement": abs(
                    float((arrival_status.get("fair") or {}).get("yes"))
                    - float(arrival["market_yes"])
                ),
            },
        )
        self.state["counterfactual_fills"] = int(self.state.get("counterfactual_fills") or 0) + 1
        self.state.setdefault("traded_markets", []).append(market_id)
        self.state.setdefault("positions", {})[position_id] = {
            "position_id": position_id, "counterfactual_id": counterfactual_id, "fill_id": fill_id,
            "order_id": order_id,
            "market_id": market_id, "event_id": str(market.get("event_id") or ""),
            "token_id": row["token_id"], "outcome": row["outcome"], "shares": size,
            "entry_price": ask, "entry_fee": total_fee, "entry_cost": cost,
            "executable_value": executable_value, "opened_ms": arrival_book.receive_ts_ms,
            "fee_schedule": schedule, "markouts": [], "settled": False,
            "model_yes": finite((arrival_status.get("fair") or {}).get("yes")),
            "market_yes": float(arrival["market_yes"]),
            "market_mid_source": "LIVE_COMPLEMENT_CONSISTENT_CLOB_BATCH",
        }
        self.last_attempt_reason = "VIRTUAL_FILL"
        return True

    def observe_positions(self) -> None:
        current_ms = now_ms()
        for position in list((self.state.get("positions") or {}).values()):
            if position.get("settled"):
                continue
            age_seconds = max(0.0, (current_ms - int(position["opened_ms"])) / 1000.0)
            due = [horizon for horizon in HORIZONS if horizon <= age_seconds and horizon not in position.get("markouts", [])]
            if due:
                book = self.fetch_book(str(position["token_id"]))
                if book is not None:
                    schedule = position.get("fee_schedule") if isinstance(position.get("fee_schedule"), dict) else {}
                    liquidation = executable_sell_value(book, float(position["shares"]), schedule)
                    position["executable_value"] = liquidation
                    per_share = (liquidation - float(position["entry_cost"]) - float(position["entry_fee"])) / float(position["shares"])
                    for horizon in due:
                        self.emit_counterfactual(
                            "VIRTUAL_MARKOUT", strategy=STRATEGY, model_version=MODEL_VERSION,
                            counterfactual_id=str(position["counterfactual_id"]),
                            fill_id=str(position["fill_id"]), position_id=str(position["position_id"]),
                            market_id=str(position["market_id"]), event_id=str(position["event_id"]),
                            token_id=str(position["token_id"]), side="BUY", exchange_ts_ms=book.exchange_ts_ms,
                            receive_ts_ms=book.receive_ts_ms, book_snapshot_id=book.snapshot_id,
                            executable_liquidation_value=liquidation, markouts={f"{horizon}s": per_share},
                            metadata={"full_visible_depth": True, "fill_conditioned": True},
                        )
                        self.emit_canonical_shadow(LedgerEvent(
                            event_type="MARKOUT", strategy=STRATEGY, model_sha=self.sha,
                            model_version=MODEL_VERSION,
                            order_id=str(position["order_id"]), fill_id=str(position["fill_id"]),
                            position_id=str(position["position_id"]), market_id=str(position["market_id"]),
                            event_id=str(position["event_id"]), token_id=str(position["token_id"]),
                            side="BUY", exchange_ts_ms=book.exchange_ts_ms,
                            receive_ts_ms=book.receive_ts_ms, book_snapshot_id=book.snapshot_id,
                            executable_liquidation_value=liquidation,
                            markouts={f"{horizon}s": per_share},
                            metadata={"model_family": STRATEGY, "horizon_seconds": 300,
                                      "full_visible_depth": True, "fill_conditioned": True},
                        ))
                        position.setdefault("markouts", []).append(horizon)
            if age_seconds < 300:
                continue
            try:
                raw = request_json(f"{self.gamma_url}/markets/{urllib.parse.quote(str(position['market_id']))}", timeout=4)
            except Exception:
                continue
            if not isinstance(raw, dict) or raw.get("closed") is not True:
                continue
            outcomes = [str(value) for value in parse_array(raw.get("outcomes"))]
            tokens = [str(value) for value in parse_array(raw.get("clobTokenIds"))]
            prices = [finite(value) for value in parse_array(raw.get("outcomePrices"))]
            winning_index = next((index for index, price in enumerate(prices)
                                  if math.isfinite(price) and price >= 1.0 - 1e-9), -1)
            if winning_index < 0 or winning_index >= len(tokens):
                continue
            winning_token = tokens[winning_index]
            resolved = outcomes[winning_index] if winning_index < len(outcomes) else ""
            payout = float(position["shares"]) if winning_token == str(position["token_id"]) else 0.0
            pnl = payout - float(position["entry_cost"]) - float(position["entry_fee"])
            self.state["counterfactual_realized_pnl"] = float(
                self.state.get("counterfactual_realized_pnl") or 0.0
            ) + pnl
            position["settled"] = True
            position["resolved_outcome"] = resolved
            won = winning_token == str(position["token_id"])
            self.emit_counterfactual(
                "VIRTUAL_FINAL", strategy=STRATEGY, model_version=MODEL_VERSION,
                counterfactual_id=str(position["counterfactual_id"]),
                position_id=str(position["position_id"]),
                fill_id=str(position["fill_id"]), market_id=str(position["market_id"]),
                event_id=str(position["event_id"]), token_id=str(position["token_id"]), side="BUY",
                counterfactual_pnl=pnl, virtual_cashflow=payout,
                capital_duration_ms=current_ms - int(position["opened_ms"]),
                metadata={
                    "settlement_outcome": resolved, "winning_token_id": winning_token,
                    "won": won, "hold_to_settlement": True, "counterfactual": True,
                    "model_yes": position.get("model_yes"),
                    "market_yes": position.get("market_yes"),
                    "model_family": STRATEGY, "horizon_seconds": 300,
                },
            )
            self.emit_canonical_shadow(LedgerEvent(
                event_type="FINAL", strategy=STRATEGY, model_sha=self.sha,
                model_version=MODEL_VERSION, order_id=str(position["order_id"]),
                position_id=str(position["position_id"]), market_id=str(position["market_id"]),
                event_id=str(position["event_id"]), token_id=str(position["token_id"]), side="BUY",
                final_pnl=pnl, realized_cashflow=pnl, fee=0.0, slippage=0.0,
                unwind_loss=0.0, capital_cost=0.0, latency_cost=0.0,
                capital_duration_ms=current_ms - int(position["opened_ms"]),
                metadata={
                    "model_family": STRATEGY, "horizon_seconds": 300,
                    "realized": True, "unwind_accounted": True,
                    "cost_vector_complete": True,
                    "terminal_id": f"external-shadow:{position['position_id']}:final",
                    "pnl_decomposition": {
                        "trading_pnl": pnl, "spread_capture": 0.0,
                        "adverse_markout": 0.0, "inventory_pnl": 0.0,
                        "maker_rebates": 0.0, "liquidity_rewards": 0.0,
                        "own_reward_share_verified": False,
                    },
                },
            ))

    def publish(self, active_candidates: int, blocker: str = "") -> None:
        positions = self.state.get("positions") if isinstance(self.state.get("positions"), dict) else {}
        open_positions = sum(1 for position in positions.values() if not position.get("settled"))
        virtual_equity = starting_capital = float(self.state.get("starting_capital") or 0.0)
        virtual_equity += float(self.state.get("counterfactual_realized_pnl") or 0.0)
        virtual_equity += sum(
            float(position.get("executable_value") or 0.0)
            - float(position.get("entry_cost") or 0.0)
            - float(position.get("entry_fee") or 0.0)
            for position in positions.values() if not position.get("settled")
        )
        peak = max(starting_capital, float(self.state.get("peak_equity") or starting_capital), virtual_equity)
        drawdown = max(0.0, 1.0 - virtual_equity / peak) if peak > 0.0 else 1.0
        killed = bool(self.state.get("killed")) or (self.root / "control" / "KILL").exists()
        drain_requested = self.drain_path.exists()
        if drain_requested:
            blocker = "CUTOVER_DRAIN"
        self.state["peak_equity"] = peak
        self.state["killed"] = killed
        atomic_json(self.state_path, self.state)
        maturity = self.maturity_diagnostics()
        atomic_json(self.status_path, {
            "schema": "polymarket_v7_external_fair_paper_router_v1", "timestamp": int(time.time()),
            "code_sha": self.sha, "state": "KILLED" if killed else "DRAINING" if drain_requested else "RUNNING", "paper_only": True,
            "authenticated_execution": False, "real_order_submission": False,
            "execution_mode": "SHADOW_COUNTERFACTUAL",
            "policy_sha256": self.policy_sha256,
            "execution_authority": "SHADOW_ZERO_AUTHORITY", "model_mature": self.model_mature,
            "economic_confidence": (
                "PAPER_PROMOTION_ELIGIBLE_MANUAL_REVIEW"
                if maturity["eligible_for_manual_paper_promotion"]
                else "MORE_EVIDENCE_REQUIRED"),
            "maturity": maturity,
            "active_candidates": active_candidates,
            "entry_tte_window_seconds": {
                "minimum": self.policy.get("minimum_entry_tte_seconds"),
                "maximum": self.policy.get("maximum_entry_tte_seconds"),
            },
            "sizing_regime": (
                "MATURE_SHADOW_FRACTIONAL_KELLY" if self.model_mature
                else "IMMATURE_SHADOW_FIXED_NOTIONAL"
            ),
            "market_capital_ceiling": float(self.state.get("starting_capital") or 0.0) * float(
                self.policy.get(
                    "max_market_capital_fraction" if self.model_mature
                    else "immature_exploration_capital_fraction",
                    0.02 if self.model_mature else 0.0025,
                )
            ),
            "counterfactual_collection_enabled": not killed and not drain_requested,
            "counterfactual_tape": str(self.counterfactual_path),
            "counterfactual_candidates": int(self.state.get("candidates") or 0),
            "candidates_spooled": 0,
            "orders_submitted": int(self.state.get("orders") or 0), "fills": int(self.state.get("fills") or 0),
            "counterfactual_fills": int(self.state.get("counterfactual_fills") or 0),
            "counterfactual_forecasts": int(self.state.get("forecasts") or 0),
            "counterfactual_resolved_forecasts": int(self.state.get("resolved_forecasts") or 0),
            "counterfactual_pending_forecasts": len(
                self.state.get("pending_forecasts")
                if isinstance(self.state.get("pending_forecasts"), dict) else {}
            ),
            "counterfactual_open_positions": open_positions,
            "counterfactual_realized_pnl": float(self.state.get("counterfactual_realized_pnl") or 0.0),
            "counterfactual_equity": virtual_equity,
            "open_positions": 0, "realized_pnl": 0.0,
            "cash": starting_capital, "equity": starting_capital,
            "counterfactual_peak_equity": peak, "counterfactual_drawdown": drawdown,
            "peak_equity": starting_capital, "drawdown": 0.0, "killed": killed,
            "order_submission_enabled": False,
            "drain_requested": drain_requested, "drain_complete": drain_requested,
            "blocker": blocker,
            "live_market": self.last_live_market,
            "book_requests": int(self.state.get("book_requests") or 0),
            "book_request_failures": int(self.state.get("book_request_failures") or 0),
            "book_parse_failures": int(self.state.get("book_parse_failures") or 0),
            "rejection_reasons": self.state.get("rejection_reasons") or {},
            "wait_reasons": self.state.get("wait_reasons") or {},
            "last_decision": self.state.get("last_decision") or {},
            "actions": {"MAKE": 0, "TAKE": 0, "CANCEL": 0,
                        "WITHDRAW": 0, "NOTHING": int(self.state.get("nothing") or 0)},
            "counterfactual_actions": {"TAKE": int(self.state.get("counterfactual_fills") or 0)},
        })

    def step(self) -> None:
        status = load(self.source)
        blocker = ""
        books: dict[str, Book] = {}
        market_yes: float | None = None
        if self.drain_path.exists():
            blocker = "CUTOVER_DRAIN"
            rows = []
        elif status.get("code_sha") != self.sha:
            blocker = "EXTERNAL_FAIR_SHA_MISMATCH"
            rows: list[dict[str, Any]] = []
        else:
            books = self.books_for(status)
            market = status.get("market") if isinstance(status.get("market"), dict) else {}
            market_yes = live_market_yes(books, market)
            book_values = list(books.values())
            self.last_live_market = {
                "market_id": str(market.get("market_id") or ""),
                "yes": market_yes,
                "valid": market_yes is not None,
                "source": "LIVE_COMPLEMENT_CONSISTENT_CLOB_BATCH",
                "receive_ts_ms": max((book.receive_ts_ms for book in book_values), default=0),
                "exchange_ts_ms": max((book.exchange_ts_ms for book in book_values), default=0),
                "snapshot_id": stable_id(*(book.snapshot_id for book in book_values)),
                "reason": "" if market_yes is not None else "CLOB_COMPLEMENT_INCOHERENT",
            }
            self.record_forecast(status, books)
            rows = robust_candidates(status, books, self.policy)
        filled = False
        for row in rows:
            if self.attempt(status, row):
                filled = True
                break
        if not filled:
            self.state["nothing"] = int(self.state.get("nothing") or 0) + 1
            if blocker:
                reason = blocker
            elif self.last_book_error:
                reason = self.last_book_error
            elif len(books) < 2:
                reason = "CLOB_BOOKS_UNAVAILABLE"
            elif market_yes is None:
                reason = "CLOB_COMPLEMENT_INCOHERENT"
            elif (status.get("fair") or {}).get("valid") and entry_tte_allowed(
                    status.get("fair") or {}, self.policy) and not model_market_disagreement_allowed(
                        status.get("fair") or {}, self.policy, market_yes):
                reason = "MODEL_MARKET_DISAGREEMENT_LIMIT"
            elif rows and self.last_attempt_reason:
                reason = self.last_attempt_reason
            elif rows:
                reason = "ROBUST_CANDIDATE_NOT_FILLED"
            elif (status.get("fair") or {}).get("valid") and not entry_tte_allowed(
                    status.get("fair") or {}, self.policy):
                reason = "ENTRY_TTE_OUTSIDE_WINDOW"
            else:
                reason = "NO_ROBUST_EV"
            if reason == "WAITING_FOR_NEXT_CONTRACT_HANDOFF":
                self.wait(reason)
            else:
                self.reject(reason)
        else:
            reason = "VIRTUAL_FILL"
        self.state["last_decision"] = {
            "timestamp_ms": now_ms(),
            "market_id": str((status.get("market") or {}).get("market_id") or ""),
            "books": len(books), "robust_candidates": len(rows), "outcome": reason,
            "live_market_yes": market_yes,
            "gamma_discovery_mid_diagnostic": (status.get("fair") or {}).get(
                "gamma_discovery_mid_diagnostic"
            ),
            "market_mid_source": (
                "LIVE_COMPLEMENT_CONSISTENT_CLOB_BATCH" if market_yes is not None else "UNAVAILABLE"
            ),
            "best_robust_ev_per_share": max((float(row["robust_ev"]) for row in rows), default=None),
        }
        self.observe_positions()
        self.observe_forecasts()
        self.publish(len(rows), blocker)

    def run(self, interval: float) -> None:
        while True:
            try:
                self.step()
            except Exception as exc:
                self.publish(0, f"ROUTER_ERROR:{type(exc).__name__}")
            time.sleep(max(0.25, interval))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--config", type=Path, default=Path("config/v7_external_fair.json"))
    parser.add_argument("--clob-url", default="https://clob.polymarket.com")
    parser.add_argument("--gamma-url", default="https://gamma-api.polymarket.com")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    if len(args.model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in args.model_sha):
        raise SystemExit("exact model SHA required")
    PaperRouter(args.run_root.resolve(), args.model_sha, args.config.resolve(), args.clob_url, args.gamma_url).run(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
