"""Causal market-level settlement-EV dataset from native decision observations.

Research-only. One preregistered accepted taker decision per market, joined only
to canonical FINAL settlement labels. No execution authority or auto-promotion.
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


def _finite(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _sha_bytes(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _rows_sha(rows: list[dict[str, Any]]) -> str:
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
                if isinstance(row, dict) and row.get("schema") == OBS_SCHEMA:
                    yield row


def settlement_labels_from_ledger(
    path: Path, *, expected_model_sha: str | None = None
) -> dict[str, dict[str, Any]]:
    labels: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            if row.get("event_type") != "FINAL" or row.get("strategy") != ENGINE:
                continue
            if row.get("paper_only") is not True:
                continue
            if expected_model_sha is not None and row.get("model_sha") != expected_model_sha:
                continue
            market = str(row.get("market_id") or "")
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            payouts = metadata.get("settlement_payouts")
            recorded_ms = int(row.get("recorded_ts_ms") or 0)
            if not market or not isinstance(payouts, dict) or recorded_ms <= 0:
                continue
            clean: dict[str, float] = {}
            for token, raw in payouts.items():
                value = _finite(raw)
                if value is None or value not in (0.0, 0.5, 1.0):
                    raise ValueError(f"invalid settlement payout:{market}")
                clean[str(token)] = value
            if len(clean) != 2:
                raise ValueError(f"settlement payout vector incomplete:{market}")
            label = {
                "token_payouts": clean,
                "observed_ns": recorded_ms * 1_000_000,
                "record_id": str(row.get("record_id") or ""),
            }
            prior = labels.get(market)
            if prior is not None and (
                prior["token_payouts"] != clean
                or prior["observed_ns"] != label["observed_ns"]
            ):
                raise ValueError(f"conflicting canonical FINAL:{market}")
            labels[market] = label
    return labels


def first_accepted_taker_per_market(
    rows: Iterable[dict[str, Any]]
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
        decision = int(row.get("decision_wall_ns") or 0)
        if not market or decision <= 0:
            continue
        key = (decision, int(row.get("sequence") or 0))
        prior = chosen.get(market)
        prior_key = (
            (int(prior.get("decision_wall_ns") or 0), int(prior.get("sequence") or 0))
            if prior else None
        )
        if prior is None or key < prior_key:
            chosen[market] = row
    return sorted(
        chosen.values(),
        key=lambda row: (int(row["decision_wall_ns"]), str(row["market_id"])),
    )


def build_rows(
    observations: Iterable[dict[str, Any]],
    labels: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    output: list[dict[str, Any]] = []
    stats = {
        "selected_markets": 0,
        "missing_labels": 0,
        "nonbinary_labels": 0,
        "invalid_rows": 0,
    }
    for row in first_accepted_taker_per_market(observations):
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
        if outcome == 0.5:
            stats["nonbinary_labels"] += 1
            continue
        bid = int(row.get("bid_e4") or 0)
        ask = int(row.get("ask_e4") or 0)
        decision_ns = int(row.get("decision_wall_ns") or 0)
        label_ns = int(label.get("observed_ns") or 0)
        if outcome not in (0.0, 1.0) or not 0 < bid <= ask < 10_000:
            stats["invalid_rows"] += 1
            continue
        if decision_ns <= 0 or label_ns <= decision_ns:
            stats["invalid_rows"] += 1
            continue

        pm_probability = (bid + ask) / 20_000.0
        executable_ask = ask / 10_000.0
        fee_rate = _finite(row.get("fee_rate"))
        exponent = _finite(row.get("fee_exponent"))
        signal = _finite(row.get("binance_return_100ms_bp", row.get("signal_return_bp")))
        confirm = _finite(row.get("coinbase_return_100ms_bp"))
        tte_ns = int(row.get("tte_ns") or 0)
        age_ns = int(row.get("signal_age_ns") or 0)
        bid_qty = max(0, int(row.get("bid_quantity") or 0))
        ask_qty = max(0, int(row.get("ask_quantity") or 0))
        if (
            None in (fee_rate, exponent, signal, confirm)
            or tte_ns <= 0
            or fee_rate < 0
            or exponent < 0
        ):
            stats["invalid_rows"] += 1
            continue

        fee_per_share = fee_rate * ((executable_ask * (1.0 - executable_ask)) ** exponent)
        imbalance = (
            (bid_qty - ask_qty) / (bid_qty + ask_qty)
            if bid_qty + ask_qty else 0.0
        )
        asset = str(row.get("asset") or "")
        horizon = str(row.get("horizon") or "")
        direction = int(row.get("direction") or 0)
        if direction not in (-1, 1):
            stats["invalid_rows"] += 1
            continue
        features: dict[str, float] = {
            "signal_return_bp": float(signal),
            "confirmation_return_bp": float(confirm),
            "abs_signal_return_bp": abs(float(signal)),
            "aligned_signal_return_bp": direction * float(signal),
            "aligned_confirmation_return_bp": direction * float(confirm),
            "tte_seconds": tte_ns / 1e9,
            "signal_age_ms": age_ns / 1e6,
            "spread": (ask - bid) / 10_000.0,
            "bid_depth_shares": bid_qty / 1_000_000.0,
            "ask_depth_shares": ask_qty / 1_000_000.0,
            "depth_imbalance": imbalance,
            "confirmed_non_opposing": 1.0 if row.get("confirmed_non_opposing") is True else 0.0,
        }
        for name in ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"):
            features[f"asset_{name}"] = 1.0 if asset == name else 0.0
        for name in ("M5", "M15", "H1", "H4", "D1"):
            features[f"horizon_{name}"] = 1.0 if horizon == name else 0.0

        output.append({
            "market": market,
            "asset": asset,
            "horizon": horizon,
            "decision_ns": decision_ns,
            "label_observed_ns": label_ns,
            "complete": True,
            "outcome": int(outcome),
            "pm_probability": pm_probability,
            "executable_ask": executable_ask,
            "fee_per_share": fee_per_share,
            "break_even_probability": executable_ask + fee_per_share,
            "features": features,
            "signal_version": int(row.get("signal_version") or 0),
            "book_version": int(row.get("book_version") or 0),
            "settlement_record_id": str(label.get("record_id") or ""),
        })
    output.sort(key=lambda row: (row["decision_ns"], row["market"]))
    stats["selected_markets"] = len(output)
    return output, stats


def dataset(
    observation_paths: Iterable[Path],
    ledger_path: Path,
    *,
    expected_model_sha: str | None = None,
) -> dict[str, Any]:
    paths = list(observation_paths)
    labels = settlement_labels_from_ledger(
        ledger_path, expected_model_sha=expected_model_sha
    )
    rows, stats = build_rows(iter_observations(paths), labels)
    return {
        "schema": SCHEMA,
        "paper_only": True,
        "execution_authority": False,
        "automatic_promotion": False,
        "rows": rows,
        "stats": {**stats, "canonical_labels": len(labels)},
        "dataset_sha256": _rows_sha(rows),
        "input_sha256": {
            "ledger": _sha_bytes(ledger_path),
            "observations": {str(path): _sha_bytes(path) for path in paths},
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observation", type=Path, action="append", required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--model-sha")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("output exists")
    report = dataset(
        args.observation, args.ledger, expected_model_sha=args.model_sha
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"stats": report["stats"], "output": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
