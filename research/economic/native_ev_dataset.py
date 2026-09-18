#!/usr/bin/env python3
"""Build a causal market-level EV dataset from native decision observations.

Research-only. Labels come only from canonical FINAL events observed after the
native decision. No production threshold is selected and no execution authority
is granted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

SCHEMA = "polymarket_v7_native_ev_dataset_v1"
OBS_SCHEMA = "polymarket_v7_native_observation_v1"
ENGINE = "CRYPTO_SETTLEMENT_ENGINE"
ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB")
HORIZONS = ("M5", "M15", "H1", "H4", "D1")


def _finite(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rows_sha256(rows: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        rows, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def iter_observations(paths: Iterable[Path]) -> Iterable[dict[str, Any]]:
    for path in paths:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    isinstance(row, dict)
                    and row.get("schema") == OBS_SCHEMA
                    and row.get("paper_only") is True
                    and row.get("execution_authority") is False
                ):
                    yield row


def labels_from_ledger(
    path: Path, *, expected_code_sha: str
) -> dict[str, dict[str, Any]]:
    if len(expected_code_sha) != 40 or any(
        ch not in "0123456789abcdef" for ch in expected_code_sha
    ):
        raise ValueError("exact code SHA required")
    labels: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                not isinstance(row, dict)
                or row.get("event_type") != "FINAL"
                or row.get("strategy") != ENGINE
                or row.get("model_sha") != expected_code_sha
                or row.get("paper_only") is not True
                or row.get("authenticated_execution") is not False
            ):
                continue
            market = str(row.get("market_id") or "")
            recorded_ms = int(row.get("recorded_ts_ms") or 0)
            metadata = (
                row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            )
            payouts = metadata.get("settlement_payouts")
            if not market or recorded_ms <= 0 or not isinstance(payouts, dict):
                continue
            normalized: dict[str, float] = {}
            valid = True
            for token, value in payouts.items():
                payout = _finite(value)
                if payout is None or not 0.0 <= payout <= 1.0:
                    valid = False
                    break
                normalized[str(token)] = payout
            if not valid or not normalized:
                continue
            candidate = {
                "token_payouts": normalized,
                "observed_ns": recorded_ms * 1_000_000,
                "asset": str(metadata.get("asset") or ""),
                "horizon": str(metadata.get("horizon") or ""),
                "record_id": str(row.get("record_id") or ""),
            }
            prior = labels.get(market)
            if prior is None:
                labels[market] = candidate
            elif prior["token_payouts"] != normalized:
                raise ValueError(f"conflicting FINAL payouts:{market}")
            elif candidate["observed_ns"] < prior["observed_ns"]:
                labels[market] = candidate
    return labels


def first_accepted_taker_per_market(
    rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    chosen: dict[str, dict[str, Any]] = {}
    for row in rows:
        if (
            int(row.get("kind") or 0) != 2
            or row.get("accepted") is not True
            or row.get("book_valid") is not True
        ):
            continue
        market = str(row.get("market_id") or "")
        if not market:
            continue
        key = (
            int(row.get("decision_wall_ns") or 0),
            int(row.get("sequence") or 0),
        )
        if key[0] <= 0:
            continue
        prior = chosen.get(market)
        prior_key = (
            (
                int(prior.get("decision_wall_ns") or 0),
                int(prior.get("sequence") or 0),
            )
            if prior
            else None
        )
        if prior is None or key < prior_key:
            chosen[market] = row
    return sorted(
        chosen.values(),
        key=lambda row: (
            int(row.get("decision_wall_ns") or 0),
            str(row.get("market_id") or ""),
        ),
    )


def build_rows(
    observations: Iterable[dict[str, Any]],
    labels: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    output: list[dict[str, Any]] = []
    stats = {
        "accepted_unique_markets": 0,
        "selected_markets": 0,
        "missing_labels": 0,
        "nonbinary_labels": 0,
        "noncausal_labels": 0,
        "invalid_economics": 0,
    }
    selected = first_accepted_taker_per_market(observations)
    stats["accepted_unique_markets"] = len(selected)
    for row in selected:
        market = str(row["market_id"])
        label = labels.get(market)
        if not isinstance(label, dict):
            stats["missing_labels"] += 1
            continue
        payouts = label.get("token_payouts")
        token = str(row.get("token_id") or "")
        if not isinstance(payouts, dict) or token not in payouts:
            stats["missing_labels"] += 1
            continue
        outcome = _finite(payouts[token])
        if outcome not in (0.0, 1.0):
            stats["nonbinary_labels"] += 1
            continue

        decision_wall_ns = int(row.get("decision_wall_ns") or 0)
        label_observed_ns = int(label.get("observed_ns") or 0)
        if (
            decision_wall_ns <= 0
            or label_observed_ns <= decision_wall_ns
        ):
            stats["noncausal_labels"] += 1
            continue

        bid = int(row.get("bid_e4") or 0)
        ask = int(row.get("ask_e4") or 0)
        fee_rate = _finite(row.get("fee_rate"))
        exponent = _finite(row.get("fee_exponent"))
        signal = _finite(row.get("signal_return_bp"))
        confirm = _finite(row.get("confirmation_return_bp"))
        direction = int(row.get("direction") or 0)
        tte_ns = int(row.get("tte_ns") or 0)
        age_ns = int(row.get("signal_age_ns") or 0)
        bq = max(0, int(row.get("bid_quantity") or 0))
        aq = max(0, int(row.get("ask_quantity") or 0))
        if (
            not 0 < bid <= ask < 10_000
            or fee_rate is None
            or exponent is None
            or signal is None
            or confirm is None
            or fee_rate < 0
            or exponent < 0
            or direction not in (-1, 1)
            or tte_ns <= 0
        ):
            stats["invalid_economics"] += 1
            continue

        pm_probability = (bid + ask) / 20_000.0
        price = ask / 10_000.0
        fee_per_share = fee_rate * ((price * (1.0 - price)) ** exponent)
        break_even = price + fee_per_share
        imbalance = (bq - aq) / (bq + aq) if bq + aq else 0.0
        asset = str(row.get("asset") or "")
        horizon = str(row.get("horizon") or "")
        if asset not in ASSETS or horizon not in HORIZONS:
            stats["invalid_economics"] += 1
            continue

        features: dict[str, float] = {
            "signal_strength_bp": abs(signal),
            "confirmation_aligned_bp": direction * confirm,
            "tte_seconds": tte_ns / 1e9,
            "signal_age_ms": max(0, age_ns) / 1e6,
            "spread": (ask - bid) / 10_000.0,
            "depth_imbalance": imbalance,
            "direction_up": 1.0 if direction > 0 else 0.0,
            "technical_signal_valid": 1.0 if row.get("signal_valid") is True else 0.0,
            "confirmed_non_opposing": (
                1.0 if row.get("confirmed_non_opposing") is True else 0.0
            ),
        }
        for name in ASSETS:
            features[f"asset_{name}"] = 1.0 if asset == name else 0.0
        for name in HORIZONS:
            features[f"horizon_{name}"] = 1.0 if horizon == name else 0.0

        output.append(
            {
                "market": market,
                "asset": asset,
                "horizon": horizon,
                "decision_ns": decision_wall_ns,
                "decision_monotonic_ns": int(
                    row.get("decision_monotonic_ns") or 0
                ),
                "label_observed_ns": label_observed_ns,
                "complete": True,
                "outcome": int(outcome),
                "pm_probability": pm_probability,
                "executable_ask": price,
                "fee_per_share": fee_per_share,
                "break_even_probability": break_even,
                "ex_post_net_per_share": outcome - price - fee_per_share,
                "features": features,
                "signal_version": int(row.get("signal_version") or 0),
                "book_version": int(row.get("book_version") or 0),
                "source_capture_id": str(row.get("capture_id") or ""),
                "source_sequence": int(row.get("sequence") or 0),
            }
        )
    output.sort(key=lambda row: (row["decision_ns"], row["market"]))
    stats["selected_markets"] = len(output)
    return output, stats


def dataset(
    observation_paths: Iterable[Path],
    labels: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    paths = sorted({Path(path) for path in observation_paths})
    rows, stats = build_rows(iter_observations(paths), labels)
    return {
        "schema": SCHEMA,
        "paper_only": True,
        "execution_authority": False,
        "automatic_promotion": False,
        "rows": rows,
        "stats": stats,
        "dataset_sha256": _rows_sha256(rows),
        "observation_sha256": {
            str(path): _file_sha256(path) for path in paths
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observation", type=Path, action="append", required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("output exists")
    labels = labels_from_ledger(args.ledger, expected_code_sha=args.code_sha)
    report = dataset(args.observation, labels)
    report["code_sha"] = args.code_sha
    report["ledger_sha256"] = _file_sha256(args.ledger)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    )
    print(
        json.dumps(
            {
                "rows": len(report["rows"]),
                "stats": report["stats"],
                "dataset_sha256": report["dataset_sha256"],
                "output": str(args.output),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
