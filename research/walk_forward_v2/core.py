"""Historical walk-forward V2 primitives.

This module is deliberately offline.  It reads immutable HFT compact objects and
PM observer windows, keeps all causal times as integer nanoseconds, and treats
missing evidence as censored rather than as a zero move, fill, or PnL.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
import gzip
import hashlib
import json
import math
from pathlib import Path
import random

from research.economic.causal_replay import cash_fee

SAFETY = {
    "paper_only": True,
    "authenticated_execution": False,
    "real_order_submission": False,
    "real_capital_at_risk": False,
}
SCHEMA = "historical_walk_forward_v2"
HORIZONS_MS = (25, 50, 100, 250, 500, 1000, 2000)
EXECUTION_LATENCIES_MS = (0, 10, 25, 50, 100, 250, 500)
TARGET_TOLERANCE_NS = 50_000_000
DEFAULT_EPOCH_NS = 1_789_921_800_000_000_000  # 2026-09-20 16:30:00 UTC
REASON_NAMES = {
    1: "Accepted", 2: "InvalidSignal", 3: "ExpiredSignal", 4: "WeakSignal",
    5: "MarketUnavailable", 6: "TteOutsideWindow", 7: "InvalidBook",
    8: "InsufficientDepth", 9: "InvalidTick", 10: "DuplicateSignal",
    11: "MarketAlreadyTraded", 12: "CapitalDenied", 13: "EntryPriceTooHigh",
    14: "MarketAlreadyRepriced", 15: "ProbabilityUnavailable",
    16: "NetEdgeNonPositive", 17: "RiskSizeBelowMinimum",
    18: "SlowContextUnavailable",
}
FUNNEL_STAGES = (
    "native_decision_rows", "valid_causal_signals", "confirmed_signals",
    "tte_valid", "fresh_book", "pm_pretrigger", "forecast_available",
    "predicted_repricing_positive", "gross_edge_positive",
    "spread_adjusted_edge_positive", "fee_adjusted_edge_positive",
    "reserve_adjusted_edge_positive", "edge_threshold",
    "price_cap", "sufficient_depth", "risk_size", "capital_admitted",
    "simulated_order", "valid_arrival_book", "limit_touched", "fill",
    "partial_or_full_fill", "positive_markout", "positive_settlement_pnl",
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def clamp_probability(value):
    return min(1 - 1e-8, max(1e-8, value))


def logit(value):
    value = clamp_probability(value)
    return math.log(value / (1 - value))


def sigmoid(value):
    return 1 / (1 + math.exp(-max(-40, min(40, value))))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(canonical(value))
    temporary.replace(path)


def json_lines(path):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            if line.endswith("\n"):
                try:
                    yield json.loads(line)
                except ValueError:
                    yield None


def source_hash(path):
    """Byte-exact immutable object identity; do not decompress large sources twice."""
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            hasher.update(block)
    return hasher.hexdigest()


def native_wall_ns(row):
    if row.get("kind") == 2 and isinstance(row.get("decision_wall_ns"), int):
        return row["decision_wall_ns"]
    observed = row.get("observed_monotonic_ns") or row.get("decision_monotonic_ns")
    close_wall, close_monotonic = row.get("close_wall_ns"), row.get("close_monotonic_ns")
    if all(isinstance(v, int) and v > 0 for v in (observed, close_wall, close_monotonic)):
        return observed + close_wall - close_monotonic
    return 0


def valid_native(row):
    """Match the actual native-observation schema.

    Runtime native observations always carry paper_only=true and
    execution_authority=false.  Older/current capture revisions do not
    necessarily duplicate the top-level authenticated_execution and
    real_order_submission flags on every observation.  When those optional
    flags are present they must still be false.
    """
    if (
        row.get("schema") != "polymarket_v7_native_observation_v1"
        or row.get("paper_only") is not True
        or row.get("execution_authority") is not False
    ):
        return False
    if "authenticated_execution" in row and row.get("authenticated_execution") is not False:
        return False
    if "real_order_submission" in row and row.get("real_order_submission") is not False:
        return False
    return True


def valid_book(row):
    try:
        return (
            row.get("schema") == "polymarket_v7_causal_book_observation_v1"
            and row.get("paper_only") is True
            and row.get("authenticated_execution") is False
            and row.get("real_order_submission") is False
            and row.get("execution_authority") == "ZERO_AUTHORITY_RESEARCH_ONLY"
            and row.get("valid") is True and row.get("lineage_continuous") is True
            and isinstance(row["receive_wall_ms"], int) and row["receive_wall_ms"] > 0
            and isinstance(row["receive_monotonic_ns"], int) and row["receive_monotonic_ns"] > 0
            and isinstance(row["connection_epoch"], int) and row["connection_epoch"] > 0
            and isinstance(row["observer_sequence"], int) and row["observer_sequence"] > 0
            and 0 < float(row["best_bid"]) < float(row["best_ask"]) < 1
            and float(row.get("ask_depth_l1") or 0) >= 0
        )
    except (KeyError, TypeError, ValueError):
        return False


def book_from_row(row):
    return {
        "market_id": str(row["market_id"]),
        "token_id": str(row["token_id"]),
        "time_ns": int(row["receive_wall_ms"]) * 1_000_000,
        "session": str(row.get("observer_session_id") or ""),
        "epoch": int(row["connection_epoch"]),
        "sequence": int(row["observer_sequence"]),
        "bid": float(row["best_bid"]),
        "ask": float(row["best_ask"]),
        "quantity": float(row.get("ask_depth_l1") or 0),
        "tick": float(row.get("tick_size") or .01),
        "features": row.get("placement_features") if isinstance(row.get("placement_features"), dict) else {},
    }


def numeric_features(row):
    result = {}
    external = row.get("external_features")
    if isinstance(external, dict):
        input_receive = external.get("input_receive_ns")
        decision = row.get("decision_monotonic_ns")
        if isinstance(input_receive, int) and isinstance(decision, int) and input_receive > decision:
            return None
        for key, value in external.items():
            if finite(value):
                result["external." + str(key)] = float(value)
    for key in (
        "binance_return_100ms_bp", "coinbase_return_100ms_bp",
        "bybit_return_100ms_bp", "signal_return_bp", "signal_age_ns",
        "tte_ns", "bid_e4", "ask_e4", "ask_quantity",
    ):
        if finite(row.get(key)):
            result[key] = float(row[key])
    return result


def native_decision(row):
    if not valid_native(row) or row.get("kind") != 2:
        return None, "invalid_native"
    if row.get("signal_valid") is not True or row.get("confirmed_non_opposing") is not True:
        return None, "not_valid_confirmed_signal"
    if row.get("book_valid") is not True:
        return None, "invalid_book"
    required = (
        "server_id", "run_id", "capture_id", "market_id", "token_id", "asset",
        "horizon", "decision_monotonic_ns", "trigger_monotonic_ns",
        "receive_monotonic_ns", "close_monotonic_ns", "close_wall_ns",
        "bid_e4", "ask_e4", "tick_e4", "fee_rate", "fee_exponent",
        "minimum_order_microunits",
    )
    if any(row.get(key) is None for key in required):
        return None, "missing_decision_fields"
    decision_ns = native_wall_ns(row)
    try:
        monotonic, trigger, received, close = (
            int(row["decision_monotonic_ns"]), int(row["trigger_monotonic_ns"]),
            int(row["receive_monotonic_ns"]), int(row["close_monotonic_ns"]),
        )
        bid, ask = int(row["bid_e4"]) / 10000, int(row["ask_e4"]) / 10000
        if (
            decision_ns <= 0 or not 0 < bid < ask < 1 or monotonic <= 0
            or max(trigger, received) > monotonic or close <= monotonic
            or monotonic - received > 100_000_000
        ):
            return None, "invalid_decision_clock_or_book"
        direction = int(row.get("direction") or 0)
        if direction not in (-1, 1):
            return None, "invalid_direction"
        identity = digest([
            row["server_id"], row["run_id"], row["capture_id"], row.get("signal_version"),
            trigger, row.get("reason"), row.get("accepted"), decision_ns,
        ])
        return {
            "decision_id": identity, "market_id": str(row["market_id"]),
            "token_id": str(row["token_id"]), "asset": str(row["asset"]),
            "horizon": str(row["horizon"]), "decision_ns": decision_ns,
            "information_end_ns": decision_ns + 2_000_000_000,
            "trigger_ns": trigger, "signal_age_ns": monotonic - trigger,
            "tte_ns": close - monotonic, "direction": direction,
            "reason": REASON_NAMES.get(int(row.get("reason") or 0), "Unknown"),
            "accepted": bool(row.get("accepted")), "signal_valid": row.get("signal_valid") is True,
            "confirmed": row.get("confirmed_non_opposing") is True,
            "book_valid": row.get("book_valid") is True,
            "pretrigger": row.get("pm_book_pre_signal") is True or row.get("require_pm_book_pre_signal") is not True,
            "bid": bid, "ask": ask, "quantity": float(row.get("ask_quantity") or 0) / 1_000_000,
            "tick": int(row["tick_e4"]) / 10000,
            "minimum": float(row["minimum_order_microunits"]) / 1_000_000,
            "fee_rate": float(row["fee_rate"]), "fee_exponent": float(row["fee_exponent"]),
            "epoch": int(row.get("connection_epoch") or 0),
            "features": numeric_features(row), "raw_probability": row.get("probability_forecast"),
            "label": None, "label_information_ns": None, "label_provenance": "UNAVAILABLE",
        }, None
    except (TypeError, ValueError, OverflowError):
        return None, "invalid_decision_fields"


def settlement_index(roots):
    labels = {}
    if isinstance(roots, (str, Path)):
        roots = [Path(roots)]
    for root in roots:
        root = Path(root)
        if root.is_symlink() or not root.is_dir():
            continue
        for path in sorted(root.rglob("*.json")):
            if path.is_symlink():
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if value.get("schema") != "v7_public_settlement_evidence_v1" or value.get("resolution_status") != "resolved":
                continue
            market = str(value.get("market_id") or "")
            information = value.get("information_ns")
            outcomes = value.get("token_outcomes")
            if not market or not isinstance(information, int) or not isinstance(outcomes, dict):
                continue
            provenance = (
                "ARCHIVED_CAUSAL_RECEIVE_TIME"
                if value.get("actual_receive_time_archived") is True else
                "RETROSPECTIVE_REPORTED_RESOLUTION_TIME"
            )
            prior = labels.get(market)
            if prior is None or information < prior["information_ns"]:
                labels[market] = {
                    "information_ns": information, "outcomes": outcomes, "provenance": provenance,
                    "reported_resolution_ns": value.get("reported_resolution_ns"),
                }
    return labels


def attach_labels(decisions, settlement_roots):
    labels = settlement_index(settlement_roots)
    for record in decisions:
        label = labels.get(record["market_id"])
        if label is None or record["token_id"] not in label["outcomes"]:
            continue
        outcome = label["outcomes"][record["token_id"]]
        if outcome not in (0, 1) or label["information_ns"] <= record["decision_ns"]:
            continue
        record["label"] = int(outcome)
        record["label_information_ns"] = int(label["information_ns"])
        record["label_provenance"] = label["provenance"]


def _record_book_candidate(record, book, target_ns, *, tolerance_ns):
    return (
        book["time_ns"] >= target_ns
        and book["time_ns"] - target_ns <= tolerance_ns
        and (not record["epoch"] or book["epoch"] == record["epoch"])
        and book["time_ns"] < record["decision_ns"] + record["tte_ns"]
    )


def book_targets(decisions, books, tolerance_ns=TARGET_TOLERANCE_NS):
    """Small in-memory helper retained for deterministic unit tests."""
    by_key = defaultdict(list)
    for book in books:
        by_key[(book["market_id"], book["token_id"])].append(book)
    times_by_key = {}
    for key, sequence in by_key.items():
        sequence.sort(key=lambda r: (r["time_ns"], r["sequence"]))
        times_by_key[key] = [book["time_ns"] for book in sequence]
    for record in decisions:
        key = (record["market_id"], record["token_id"])
        sequence = by_key.get(key, [])
        times = times_by_key.get(key, [])
        targets = {}
        for horizon in HORIZONS_MS:
            target = record["decision_ns"] + horizon * 1_000_000
            index = bisect_left(times, target)
            if index == len(sequence):
                targets[str(horizon)] = {"state": "UNAVAILABLE_NO_POST_BOOK"}
                continue
            future = sequence[index]
            if future["time_ns"] - target > tolerance_ns:
                targets[str(horizon)] = {"state": "UNAVAILABLE_TOLERANCE"}
                continue
            if record["epoch"] and future["epoch"] != record["epoch"]:
                targets[str(horizon)] = {"state": "UNAVAILABLE_EPOCH_CHANGED"}
                continue
            if future["time_ns"] >= record["decision_ns"] + record["tte_ns"]:
                targets[str(horizon)] = {"state": "UNAVAILABLE_MARKET_EXPIRED"}
                continue
            mid0, mid1 = (record["bid"] + record["ask"]) / 2, (future["bid"] + future["ask"]) / 2
            targets[str(horizon)] = {
                "state": "OBSERVED", "observed_time_ns": future["time_ns"],
                "mid_change": mid1 - mid0, "ask_change": future["ask"] - record["ask"],
                "bid_change": future["bid"] - record["bid"], "arrival_ask": future["ask"],
                "arrival_bid": future["bid"], "arrival_quantity": future["quantity"],
            }
        record["targets"] = targets
        record["timeline"] = sequence
        record["timeline_times"] = times


def attach_streamed_book_evidence(decisions, paths, *, tolerance_ns=TARGET_TOLERANCE_NS):
    """Stream large PM windows and retain only snapshots V2 can actually use."""
    by_key = defaultdict(list)
    for record in decisions:
        record["targets"] = {}
        record["arrivals"] = {}
        by_key[(record["market_id"], record["token_id"])].append(record)
    decision_times = {}
    for key, rows in by_key.items():
        rows.sort(key=lambda row: (row["decision_ns"], row["decision_id"]))
        decision_times[key] = [row["decision_ns"] for row in rows]

    observed_books = matched = 0
    for path in paths:
        for raw in json_lines(path):
            if raw is None or raw.get("schema") != "polymarket_v7_causal_book_observation_v1":
                continue
            if not valid_book(raw):
                continue
            book = book_from_row(raw)
            key = (book["market_id"], book["token_id"])
            rows = by_key.get(key)
            if not rows:
                continue
            observed_books += 1
            times = decision_times[key]
            for horizon in HORIZONS_MS:
                delta = horizon * 1_000_000
                left = bisect_left(times, book["time_ns"] - delta - tolerance_ns)
                right = bisect_right(times, book["time_ns"] - delta)
                for record in rows[left:right]:
                    target = record["decision_ns"] + delta
                    if not _record_book_candidate(record, book, target, tolerance_ns=tolerance_ns):
                        continue
                    current = record["targets"].get(str(horizon))
                    if current and current.get("observed_time_ns", 1 << 63) <= book["time_ns"]:
                        continue
                    mid0 = (record["bid"] + record["ask"]) / 2
                    mid1 = (book["bid"] + book["ask"]) / 2
                    record["targets"][str(horizon)] = {
                        "state": "OBSERVED", "observed_time_ns": book["time_ns"],
                        "mid_change": mid1 - mid0, "ask_change": book["ask"] - record["ask"],
                        "bid_change": book["bid"] - record["bid"], "arrival_ask": book["ask"],
                        "arrival_bid": book["bid"], "arrival_quantity": book["quantity"],
                    }
                    matched += 1
            for latency in EXECUTION_LATENCIES_MS:
                delta = latency * 1_000_000
                left = bisect_left(times, book["time_ns"] - delta - tolerance_ns)
                right = bisect_right(times, book["time_ns"] - delta)
                for record in rows[left:right]:
                    target = record["decision_ns"] + delta
                    if not _record_book_candidate(record, book, target, tolerance_ns=tolerance_ns):
                        continue
                    current = record["arrivals"].get(str(latency))
                    if current and current["time_ns"] <= book["time_ns"]:
                        continue
                    record["arrivals"][str(latency)] = {
                        "time_ns": book["time_ns"], "bid": book["bid"], "ask": book["ask"],
                        "quantity": book["quantity"], "epoch": book["epoch"],
                    }

    short_pairs = arrival_pairs = 0
    for record in decisions:
        for horizon in HORIZONS_MS:
            key = str(horizon)
            if key not in record["targets"]:
                record["targets"][key] = {"state": "UNAVAILABLE_NO_POST_BOOK"}
            elif record["targets"][key].get("state") == "OBSERVED":
                short_pairs += 1
        arrival_pairs += len(record["arrivals"])
    return {
        "observed_book_rows_for_signal_keys": observed_books,
        "short_horizon_observed_pairs": short_pairs,
        "arrival_observed_pairs": arrival_pairs,
        "matched_target_updates": matched,
    }


def build_dataset(root, *, minimum_wall_ns=DEFAULT_EPOCH_NS, settlement_root=None):
    """Load causal signals first; stream PM evidence without materializing the full tape."""
    supplied_root = Path(root)
    run_layout = supplied_root / "research" / "hft_permanent"
    if run_layout.is_dir():
        hft_root = run_layout
        default_settlement_root = supplied_root / "research" / "public_settlements"
    else:
        hft_root = supplied_root
        default_settlement_root = (
            supplied_root.parent / "public_settlements"
            if supplied_root.name == "hft_permanent"
            else supplied_root / "public_settlements"
        )
    if settlement_root is not None:
        label_roots = [Path(settlement_root)]
    else:
        label_roots = [default_settlement_root]
        if run_layout.is_dir():
            archives = supplied_root.parent / "paper_v7_london_archives"
            if archives.is_dir() and not archives.is_symlink():
                label_roots.extend(
                    path / "research" / "public_settlements"
                    for path in sorted(archives.glob("cutover-*"))
                    if path.is_dir() and not path.is_symlink()
                )

    result = {"schema": SCHEMA + "_data_v1", **SAFETY, "root": str(supplied_root),
              "hft_root": str(hft_root), "settlement_roots": [str(path) for path in label_roots],
              "minimum_wall_ns": minimum_wall_ns, "sources": [], "decisions": [],
              "exclusions": Counter(), "input_state": "READY", "book_evidence": {}}
    compact = sorted(list((hft_root / "compact").glob("*.jsonl*"))
                     + list((hft_root / "compact_closed").glob("*.jsonl*")))
    windows = sorted((hft_root / "windows").glob("*.jsonl*"))
    seen_sources, seen_decisions = set(), set()

    for path in compact:
        if path.is_symlink() or not path.is_file():
            continue
        content = source_hash(path)
        if content in seen_sources:
            result["exclusions"]["DUPLICATE_SOURCE_OBJECT"] += 1
            continue
        seen_sources.add(content)
        result["sources"].append({"path": str(path.relative_to(hft_root)), "sha256": content})
        for row in json_lines(path):
            if row is None:
                result["exclusions"]["INVALID_JSON"] += 1
                continue
            if row.get("schema") != "polymarket_v7_native_observation_v1":
                continue
            if row.get("kind") == 2:
                result["exclusions"]["NATIVE_DECISION_ROWS_TOTAL"] += 1
            if native_wall_ns(row) < minimum_wall_ns:
                result["exclusions"]["PRE_EPOCH_NATIVE"] += 1
                continue
            decision, why = native_decision(row)
            if decision is None:
                result["exclusions"][why] += 1
            elif decision["decision_id"] in seen_decisions:
                result["exclusions"]["DUPLICATE_DECISION"] += 1
            else:
                seen_decisions.add(decision["decision_id"])
                result["decisions"].append(decision)

    result["decisions"].sort(key=lambda r: (r["decision_ns"], r["decision_id"]))
    attach_labels(result["decisions"], label_roots)

    unique_windows = []
    for path in windows:
        if path.is_symlink() or not path.is_file():
            continue
        content = source_hash(path)
        if content in seen_sources:
            result["exclusions"]["DUPLICATE_SOURCE_OBJECT"] += 1
            continue
        seen_sources.add(content)
        result["sources"].append({"path": str(path.relative_to(hft_root)), "sha256": content})
        unique_windows.append(path)
    if result["decisions"]:
        result["book_evidence"] = attach_streamed_book_evidence(result["decisions"], unique_windows)

    result["exclusions"] = dict(result["exclusions"])
    if not hft_root.is_dir():
        result["input_state"] = "ROOT_UNAVAILABLE"
    elif not result["decisions"]:
        result["input_state"] = "NO_ADMISSIBLE_NATIVE_DECISIONS"
    elif result["book_evidence"].get("short_horizon_observed_pairs", 0) == 0:
        result["input_state"] = "NO_ADMISSIBLE_PM_BOOK_WINDOWS"
    result["data_sha256"] = digest({
        "minimum_wall_ns": minimum_wall_ns, "sources": result["sources"],
        "decision_ids": [r["decision_id"] for r in result["decisions"]],
        "book_evidence": result["book_evidence"],
    })
    return result


def folds(records, *, desired_folds=3, embargo_ns=2_000_000_000):
    """Expanding whole-market folds. Boundaries use market starts, never PnL."""
    starts = {}
    for row in records:
        starts[row["market_id"]] = min(starts.get(row["market_id"], row["decision_ns"]), row["decision_ns"])
    markets = sorted(starts, key=lambda key: (starts[key], key))
    if len(markets) < 6:
        return [], {"state": "INSUFFICIENT_MARKETS", "markets": len(markets)}
    parts = max(2, min(desired_folds + 1, len(markets) // 2))
    cutpoints = [markets[len(markets) * index // parts:len(markets) * (index + 1) // parts] for index in range(parts)]
    output = []
    for index in range(1, len(cutpoints)):
        train_markets = {m for group in cutpoints[:index] for m in group}
        test_markets = set(cutpoints[index])
        cutoff = min(starts[m] for m in test_markets)
        historical = [
            row for row in records
            if row["market_id"] in train_markets
            and row["information_end_ns"] + embargo_ns < cutoff
        ]
        train_settlement = [
            row for row in historical
            if row["label"] is not None and row["label_information_ns"] < cutoff
        ]
        train_repricing = historical
        test = [row for row in records if row["market_id"] in test_markets]
        if train_repricing and test:
            output.append({"fold": index, "cutoff_ns": cutoff,
                           "train_settlement": train_settlement,
                           "train_repricing": train_repricing, "test": test,
                           "train_markets": sorted(train_markets), "test_markets": sorted(test_markets)})
    return output, {"state": "READY" if output else "INSUFFICIENT_PURGED_FOLDS",
                    "markets": len(markets), "embargo_ns": embargo_ns,
                    "partition_rule": "WHOLE_MARKET_START_CHRONOLOGICAL_EXPANDING"}


class Ridge:
    """Small deterministic ridge model; preprocessing is fitted only on train.

    Linear algebra is vectorized because the real London evidence contains
    hundreds of thousands of rows. This is mathematically identical to the
    previous normal-equation implementation but avoids Python O(n*p^2) loops.
    """
    def __init__(self, names, ridge=4.0):
        self.names, self.ridge = tuple(names), ridge

    def fit(self, rows, target):
        import numpy as np
        values = {name: [r["features"].get(name) for r in rows if finite(r["features"].get(name))] for name in self.names}
        self.center = {name: (sum(v) / len(v) if v else 0.0) for name, v in values.items()}
        self.scale = {name: max(1e-9, (max(v) - min(v)) / 2) if v else 1.0 for name, v in values.items()}
        X = self.matrix(rows)
        y = np.asarray([float(target(row)) for row in rows], dtype=float)
        penalty = np.eye(X.shape[1], dtype=float) * self.ridge
        penalty[0, 0] = 0.0
        self.beta = np.linalg.solve(X.T @ X + penalty, X.T @ y)
        return self

    def row(self, record):
        values = [1.0]
        for name in self.names:
            value = record["features"].get(name)
            values.append(((float(value) if finite(value) else self.center[name]) - self.center[name]) / self.scale[name])
        values.extend(float(not finite(record["features"].get(name))) for name in self.names)
        return values

    def matrix(self, records):
        import numpy as np
        return np.asarray([self.row(record) for record in records], dtype=float)

    def predict(self, record):
        return float(sum(a * b for a, b in zip(self.beta, self.row(record))))

    def predict_many(self, records):
        if not records:
            return []
        return [float(value) for value in self.matrix(records) @ self.beta]


def solve(matrix, vector):
    """Deterministic Gaussian elimination with a tiny diagonal fallback."""
    rows = [list(row) + [value] for row, value in zip(matrix, vector)]
    n = len(rows)
    for column in range(n):
        pivot = max(range(column, n), key=lambda row: abs(rows[row][column]))
        if abs(rows[pivot][column]) < 1e-12:
            rows[pivot][column] = 1e-9
        rows[column], rows[pivot] = rows[pivot], rows[column]
        scale = rows[column][column]
        rows[column] = [value / scale for value in rows[column]]
        for row in range(n):
            if row == column:
                continue
            factor = rows[row][column]
            rows[row] = [a - factor * b for a, b in zip(rows[row], rows[column])]
    return [row[-1] for row in rows]


def feature_names(records):
    names = sorted({name for row in records for name, value in row["features"].items() if finite(value)})
    return names[:24]  # fixed, deterministic capacity limit; not tuned on outcomes


def settlement_predictors(train, test):
    """Reuse the existing settlement families without making PM depend on labels.

    The PM baseline is the contemporaneous midpoint and is therefore available
    causally for every valid test row without fitting. Learned correction
    families remain unavailable until resolved labels exist strictly before the
    fold cutoff.
    """
    output = {
        "pm": [(row["bid"] + row["ask"]) / 2 for row in test],
        "logistic_offset": [None] * len(test),
        "boosted_offset": [None] * len(test),
    }
    if not train:
        return output, {"state": "PM_BASELINE_ONLY_INSUFFICIENT_SETTLEMENT_TRAINING",
                        "families": ["pm"], "training_cutoff_ns": None,
                        "failures": {"logistic_offset": "INSUFFICIENT_TRAINING",
                                     "boosted_offset": "INSUFFICIENT_TRAINING"}}
    try:
        from research.learning.models import Candidate, FAMILIES
    except ImportError:
        return output, {"state": "PM_BASELINE_ONLY_EXISTING_FAMILIES_UNAVAILABLE",
                        "families": ["pm"], "training_cutoff_ns": None,
                        "failures": {"logistic_offset": "IMPORT_ERROR",
                                     "boosted_offset": "IMPORT_ERROR"}}
    cutoff = min(row["decision_ns"] for row in test) if test else max(row["decision_ns"] for row in train) + 1
    def adapt(row):
        return {
            "market_id": row["market_id"], "signal_id": row["decision_id"],
            "asset": row["asset"], "horizon": row["horizon"], "decision_ns": row["decision_ns"],
            "information_end_ns": row["information_end_ns"], "label_information_ns": row["label_information_ns"],
            "outcome": row["label"], "pm_probability": (row["bid"] + row["ask"]) / 2,
            "features": row["features"],
        }
    train_rows, test_rows = [adapt(row) for row in train], [adapt(row) for row in test]
    failures = {}
    for family in FAMILIES:
        if family == "pm":
            continue
        try:
            model = Candidate(family, ridge=8.0, weighting="market", seed=20260920).fit(train_rows, cutoff)
            output[family] = [float(value) for value in model.predict(test_rows)]
        except (ValueError, RuntimeError, Warning) as exc:
            output[family] = [None] * len(test)
            failures[family] = type(exc).__name__ + ":" + str(exc)
    return output, {
        "state": "READY" if not failures else "PARTIAL_EXISTING_MODEL_FAILURE",
        "families": list(FAMILIES), "training_cutoff_ns": cutoff,
        "preprocessing": "EXISTING_CANDIDATE_TRAIN_ONLY_ROBUST_SCALER_WITH_MISSINGNESS",
        "failures": failures,
    }


def repricing_predictors(train, test):
    names = feature_names(train)
    out = {str(h): [None] * len(test) for h in HORIZONS_MS}
    details = {}
    for horizon in HORIZONS_MS:
        eligible = [row for row in train if row.get("targets", {}).get(str(horizon), {}).get("state") == "OBSERVED"]
        if len(eligible) < 8:
            details[str(horizon)] = {"state": "INSUFFICIENT_TRAINING_TARGETS", "rows": len(eligible)}
            continue
        model = Ridge(names, ridge=8.0).fit(eligible, lambda row: row["targets"][str(horizon)]["mid_change"])
        out[str(horizon)] = model.predict_many(test)
        details[str(horizon)] = {"state": "READY", "rows": len(eligible), "feature_names": names,
                                 "target": "future_observed_pm_midpoint_change"}
    return out, details


def walk_forward(records, *, desired_folds=3):
    all_folds, receipt = folds(records, desired_folds=desired_folds)
    evaluations = []
    for fold in all_folds:
        settlement, settlement_meta = settlement_predictors(fold["train_settlement"], fold["test"])
        repricing, repricing_meta = repricing_predictors(fold["train_repricing"], fold["test"])
        for index, row in enumerate(fold["test"]):
            evaluations.append({
                "fold": fold["fold"], "cutoff_ns": fold["cutoff_ns"], "decision_id": row["decision_id"],
                "market_id": row["market_id"], "asset": row["asset"], "horizon": row["horizon"],
                "decision_ns": row["decision_ns"], "row": row,
                "settlement_predictions": {key: values[index] for key, values in settlement.items()},
                "repricing_predictions": {key: values[index] for key, values in repricing.items()},
            })
        settlement_ids = [row["decision_id"] for row in fold["train_settlement"]]
        repricing_ids = [row["decision_id"] for row in fold["train_repricing"]]
        test_ids = [row["decision_id"] for row in fold["test"]]
        fold["train_settlement_rows"] = len(settlement_ids)
        fold["train_repricing_rows"] = len(repricing_ids)
        fold["test_rows"] = len(test_ids)
        fold["train_settlement_sha256"] = digest(settlement_ids)
        fold["train_repricing_sha256"] = digest(repricing_ids)
        fold["test_decision_sha256"] = digest(test_ids)
        fold.pop("train_settlement")
        fold.pop("train_repricing")
        fold.pop("test")
        fold["settlement"] = settlement_meta
        fold["repricing"] = repricing_meta
    return evaluations, {"schema": SCHEMA + "_folds_v1", **SAFETY, "receipt": receipt, "folds": all_folds,
                         "oos_predictions": len(evaluations)}


def fee_per_share(row, price):
    return cash_fee(1_000_000, round(price * 10000), row["fee_rate"], row["fee_exponent"])


def arrival(row, latency_ms):
    cached = (row.get("arrivals") or {}).get(str(int(latency_ms)))
    if cached is not None:
        return cached, None
    timeline = row.get("timeline") or []
    target = row["decision_ns"] + int(latency_ms) * 1_000_000
    times = row.get("timeline_times")
    if times is None:
        times = [entry["time_ns"] for entry in timeline]
    index = bisect_left(times, target)
    if index == len(timeline):
        return None, "UNAVAILABLE_NO_POST_LATENCY_BOOK"
    book = timeline[index]
    if book["time_ns"] - target > TARGET_TOLERANCE_NS:
        return None, "UNAVAILABLE_LATENCY_TOLERANCE"
    if row["epoch"] and book["epoch"] != row["epoch"]:
        return None, "UNAVAILABLE_EPOCH_CHANGED"
    if book["time_ns"] >= row["decision_ns"] + row["tte_ns"]:
        return None, "UNAVAILABLE_MARKET_EXPIRED"
    return book, None


def replay_one(row, prediction, repricing, *, latency_ms, edge_threshold=.005, entry_cap=.75, shares=5.0,
               execution_reserve=.005, ideal="REALISTIC", valuation_mode="SETTLEMENT",
               market_available=True, capital_available=True):
    """Same L1 taker economics for every candidate; unavailable is never a nonfill."""
    funnel = {stage: False for stage in FUNNEL_STAGES}
    funnel["native_decision_rows"] = True
    funnel["valid_causal_signals"] = row["signal_valid"]
    funnel["confirmed_signals"] = funnel["valid_causal_signals"] and row["confirmed"]
    funnel["tte_valid"] = funnel["confirmed_signals"] and 30_000_000_000 <= row["tte_ns"] <= 120_000_000_000
    funnel["fresh_book"] = funnel["tte_valid"] and row["book_valid"]
    funnel["pm_pretrigger"] = funnel["fresh_book"] and row["pretrigger"]
    outcome = {"status": "NO_SIGNAL", "funnel": funnel, "filled": 0.0, "pnl": None, "markout": None}
    if not funnel["pm_pretrigger"] or prediction is None:
        return outcome
    if not market_available:
        outcome["status"] = "FILTERED_MARKET_ALREADY_TRADED"
        return outcome
    funnel["forecast_available"] = True
    midpoint = (row["bid"] + row["ask"]) / 2
    if valuation_mode == "REPRICING":
        if repricing is None:
            return outcome
        expected = min(.9999, max(.0001, midpoint + repricing))
    elif valuation_mode == "SETTLEMENT_WITH_REPRICING_CONFIRMATION":
        if repricing is None:
            return outcome
        expected = prediction
    else:
        expected = prediction
    decision_price = midpoint if ideal == "NO_SPREAD_FEE_EXECUTION_UPPER_BOUND" else row["ask"]
    fee = 0.0 if ideal == "NO_SPREAD_FEE_EXECUTION_UPPER_BOUND" else fee_per_share(row, decision_price)
    gross = expected - decision_price
    after_fee = gross - fee
    after_reserve = after_fee - execution_reserve
    funnel["predicted_repricing_positive"] = repricing is None or repricing > 0
    funnel["gross_edge_positive"] = gross > 0
    funnel["spread_adjusted_edge_positive"] = gross > 0
    funnel["fee_adjusted_edge_positive"] = after_fee > 0
    funnel["reserve_adjusted_edge_positive"] = after_reserve > 0
    funnel["edge_threshold"] = after_reserve >= edge_threshold
    funnel["price_cap"] = row["ask"] <= entry_cap
    requested = min(shares, row["quantity"])
    funnel["sufficient_depth"] = requested >= row["minimum"]
    funnel["risk_size"] = funnel["sufficient_depth"]
    funnel["capital_admitted"] = funnel["risk_size"] and capital_available
    required_gates = ["edge_threshold", "price_cap", "sufficient_depth", "risk_size", "capital_admitted"]
    if valuation_mode == "SETTLEMENT_WITH_REPRICING_CONFIRMATION":
        required_gates.append("predicted_repricing_positive")
    if not all(funnel[key] for key in required_gates):
        outcome.update({"status": "FILTERED", "predicted_edge": after_reserve})
        return outcome
    funnel["simulated_order"] = True
    outcome["requested"] = requested
    outcome["reserved_cost"] = min(3.75, requested * entry_cap)
    if ideal == "PERFECT_FILL_AT_CAUSAL_DECISION_ASK_UPPER_BOUND":
        book = {"ask": row["ask"], "bid": row["bid"], "quantity": requested, "time_ns": row["decision_ns"]}
    else:
        book, why = arrival(row, 0 if ideal == "ZERO_LATENCY_EXECUTION_UPPER_BOUND" else latency_ms)
        if book is None:
            outcome.update({"status": why, "predicted_edge": after_reserve})
            return outcome
    funnel["valid_arrival_book"] = True
    limit = min(entry_cap, row["ask"] + 2 * row["tick"])
    funnel["limit_touched"] = book["ask"] <= limit
    if not funnel["limit_touched"]:
        outcome.update({"status": "NO_FILL_LIMIT_NOT_TOUCHED", "predicted_edge": after_reserve})
        return outcome
    filled = min(requested, book["quantity"])
    if filled <= 0:
        outcome.update({"status": "NO_FILL_ZERO_VISIBLE_DEPTH", "predicted_edge": after_reserve})
        return outcome
    funnel["fill"] = funnel["partial_or_full_fill"] = True
    execution_price = (book["bid"] + book["ask"]) / 2 if ideal == "NO_SPREAD_FEE_EXECUTION_UPPER_BOUND" else book["ask"]
    cost_fee = 0.0 if ideal == "NO_SPREAD_FEE_EXECUTION_UPPER_BOUND" else fee_per_share(row, execution_price) * filled
    turnover = filled * execution_price
    target = row.get("targets", {}).get("250", {})
    if target.get("state") == "OBSERVED":
        outcome["markout"] = filled * target["arrival_bid"] - turnover - cost_fee
        funnel["positive_markout"] = outcome["markout"] > 0
    if row["label"] is not None:
        outcome["pnl"] = filled * row["label"] - turnover - cost_fee
        funnel["positive_settlement_pnl"] = outcome["pnl"] > 0
    outcome.update({"status": "PARTIAL_FILL" if filled < requested else "FILLED", "filled": filled,
                    "requested": requested, "fees": cost_fee, "turnover": turnover,
                    "arrival_price": book["ask"], "predicted_edge": after_reserve,
                    "gross_edge": gross, "fee_adjusted_edge": after_fee})
    return outcome


def replay_policy(evaluations, selector, *, latency_ms, valuation_mode,
                  edge_threshold=.005, entry_cap=.75, shares=5.0,
                  execution_reserve=.005, ideal="REALISTIC",
                  capital_budget=1000.0):
    """Sequential one-entry-per-market PAPER replay with bounded capital reservation.

    A market is consumed when an order is actually simulated, matching the
    current one-entry-per-market research contract. Capital is conservatively
    reserved at the same per-order ceiling used by the existing simple backtest
    and is not recycled from later settlement information.
    """
    used_markets = set()
    reserved = 0.0
    outcomes = []
    for event in sorted(evaluations, key=lambda value: (value["decision_ns"], value["decision_id"])):
        row = event["row"]
        prediction, repricing = selector(event)
        available = row["market_id"] not in used_markets
        capital_available = reserved + min(3.75, shares * entry_cap) <= capital_budget + 1e-12
        outcome = replay_one(
            row, prediction, repricing, latency_ms=latency_ms,
            edge_threshold=edge_threshold, entry_cap=entry_cap, shares=shares,
            execution_reserve=execution_reserve, ideal=ideal,
            valuation_mode=valuation_mode, market_available=available,
            capital_available=capital_available,
        )
        outcome["market_id"], outcome["asset"], outcome["horizon"] = (
            row["market_id"], row["asset"], row["horizon"])
        outcome["decision_ns"] = row["decision_ns"]
        if outcome["funnel"]["simulated_order"]:
            used_markets.add(row["market_id"])
            reserved += float(outcome.get("reserved_cost") or 0.0)
        outcomes.append(outcome)
    return outcomes


def summarize(outcomes):
    fills = [row for row in outcomes if row["filled"] > 0]
    known = [row for row in fills if row["pnl"] is not None]
    censored = [row for row in outcomes if row["status"].startswith("UNAVAILABLE")]
    funnel = {stage: sum(bool(row["funnel"][stage]) for row in outcomes) for stage in FUNNEL_STAGES}
    return {
        "opportunities": len(outcomes), "simulated_orders": funnel["simulated_order"], "fills": len(fills),
        "partial_fills": sum(row["status"] == "PARTIAL_FILL" for row in fills),
        "censored_execution": len(censored), "fill_rate": len(fills) / funnel["simulated_order"] if funnel["simulated_order"] else None,
        "settled_fills": len(known), "net_pnl": sum(row["pnl"] for row in known) if len(known) == len(fills) else None,
        "observed_net_pnl": sum(row["pnl"] for row in known) if known else None,
        "markout_pnl": sum(row["markout"] for row in fills if row["markout"] is not None) if any(row["markout"] is not None for row in fills) else None,
        "fees": sum(row.get("fees", 0) for row in fills), "turnover": sum(row.get("turnover", 0) for row in fills),
        "pnl_per_fill": sum(row["pnl"] for row in known) / len(known) if known else None,
        "funnel": funnel, "status_counts": dict(Counter(row["status"] for row in outcomes)),
    }


def pm_edge_distribution(evaluations, execution_reserve=.005):
    by_model = defaultdict(list)
    for evaluation in evaluations:
        row = evaluation["row"]
        pm = evaluation["settlement_predictions"]["pm"]
        for model, value in evaluation["settlement_predictions"].items():
            if value is None:
                continue
            fee = fee_per_share(row, row["ask"])
            by_model[model].append({"before_cost": value - row["ask"], "after_fee": value - row["ask"] - fee,
                                    "after_reserve": value - row["ask"] - fee - execution_reserve})
        for horizon, value in evaluation["repricing_predictions"].items():
            if value is not None:
                by_model["repricing_" + horizon].append({"before_cost": value, "after_fee": value - fee_per_share(row, row["ask"]),
                                                          "after_reserve": value - fee_per_share(row, row["ask"]) - execution_reserve})
    thresholds = (0, .001, .0025, .005, .01, .02)
    result = {}
    for model, values in by_model.items():
        result[model] = {
            "rows": len(values),
            "fractions": {str(t): sum(row["after_reserve"] > t for row in values) / len(values) if values else None for t in thresholds},
            "quantiles": {key: quantile([row[key] for row in values]) for key in ("before_cost", "after_fee", "after_reserve")},
        }
    return result


def quantile(values):
    if not values:
        return None
    ordered = sorted(values)
    return {str(q): ordered[round((len(ordered) - 1) * q)] for q in (.01, .05, .5, .95, .99)}


def market_bootstrap(outcomes, *, seed=20260920, draws=256):
    groups = defaultdict(list)
    for row in outcomes:
        if row["pnl"] is not None:
            groups[row.get("market_id", "")].append(row["pnl"])
    keys = sorted(key for key, value in groups.items() if value)
    if len(keys) < 4:
        return {"state": "INSUFFICIENT_MARKET_BLOCKS", "markets": len(keys), "interval": None}
    rng = random.Random(seed)
    samples = []
    for _ in range(draws):
        chosen = [keys[rng.randrange(len(keys))] for _ in keys]
        pnl = sum(sum(groups[key]) for key in chosen)
        fills = sum(len(groups[key]) for key in chosen)
        samples.append(pnl / fills if fills else 0.0)
    return {"state": "READY", "markets": len(keys), "draws": draws,
            "pnl_per_fill_interval": [sorted(samples)[int(.025 * draws)], sorted(samples)[int(.975 * draws)]]}


def prediction_quality(evaluations):
    settlement = {}
    for model in ("pm", "logistic_offset", "boosted_offset"):
        pairs = [(event["settlement_predictions"].get(model), event["row"].get("label"))
                 for event in evaluations]
        pairs = [(float(p), int(y)) for p, y in pairs if p is not None and y in (0, 1)]
        if not pairs:
            settlement[model] = {"state": "INSUFFICIENT_LABELED_OOS", "rows": 0}
            continue
        probabilities = [clamp_probability(p) for p, _ in pairs]
        outcomes = [y for _, y in pairs]
        log_loss = -sum(y * math.log(p) + (1-y) * math.log(1-p)
                        for p, y in zip(probabilities, outcomes)) / len(pairs)
        brier = sum((p-y) ** 2 for p, y in zip(probabilities, outcomes)) / len(pairs)
        settlement[model] = {"state": "READY", "rows": len(pairs),
                             "log_loss": log_loss, "brier": brier}
    repricing = {}
    for horizon in HORIZONS_MS:
        pairs = []
        key = str(horizon)
        for event in evaluations:
            predicted = event["repricing_predictions"].get(key)
            target = event["row"].get("targets", {}).get(key, {})
            if predicted is None or target.get("state") != "OBSERVED":
                continue
            pairs.append((float(predicted), float(target["mid_change"])))
        if not pairs:
            repricing[key] = {"state": "INSUFFICIENT_OOS_TARGETS", "rows": 0}
            continue
        errors = [p-y for p, y in pairs]
        repricing[key] = {
            "state": "READY", "rows": len(pairs),
            "mae": sum(abs(e) for e in errors) / len(errors),
            "mse": sum(e*e for e in errors) / len(errors),
            "directional_accuracy": sum((p > 0) == (y > 0) for p, y in pairs) / len(pairs),
            "actual_mean_move": sum(y for _, y in pairs) / len(pairs),
            "predicted_mean_move": sum(p for p, _ in pairs) / len(pairs),
        }
    return {"settlement": settlement, "repricing": repricing}


def economic_evaluation(evaluations, *, latency_ms=(10, 25, 50, 100, 250, 500)):
    """Apply identical replay to every OOS candidate and diagnostic upper bound."""
    result = {"schema": SCHEMA + "_economics_v1", **SAFETY, "models": {}, "latency": {},
              "pm_edge_distribution": pm_edge_distribution(evaluations),
              "prediction_metrics": prediction_quality(evaluations)}
    variants = {
        "pm": (lambda event: (event["settlement_predictions"]["pm"], None), "SETTLEMENT"),
        "logistic_offset": (lambda event: (event["settlement_predictions"]["logistic_offset"], None), "SETTLEMENT"),
        "boosted_offset": (lambda event: (event["settlement_predictions"]["boosted_offset"], None), "SETTLEMENT"),
        "repricing_250ms": (lambda event: ((event["row"]["bid"] + event["row"]["ask"]) / 2,
                                           event["repricing_predictions"].get("250")), "REPRICING"),
        "combined_settlement_repricing": (lambda event: (event["settlement_predictions"]["logistic_offset"],
                                                          event["repricing_predictions"].get("250")),
                                           "SETTLEMENT_WITH_REPRICING_CONFIRMATION"),
    }
    for name, (selector, valuation_mode) in variants.items():
        outcomes = replay_policy(evaluations, selector, latency_ms=100,
                                 valuation_mode=valuation_mode)
        result["models"][name] = {"metrics": summarize(outcomes), "uncertainty": market_bootstrap(outcomes), "outcomes": outcomes}
    combined_selector = variants["combined_settlement_repricing"][0]
    for latency in latency_ms:
        outcomes = replay_policy(
            evaluations, combined_selector, latency_ms=latency,
            valuation_mode="SETTLEMENT_WITH_REPRICING_CONFIRMATION")
        result["latency"][str(latency)] = summarize(outcomes)
    upper = {}
    for kind in ("ZERO_LATENCY_EXECUTION_UPPER_BOUND", "NO_SPREAD_FEE_EXECUTION_UPPER_BOUND",
                 "PERFECT_FILL_AT_CAUSAL_DECISION_ASK_UPPER_BOUND"):
        outcomes = replay_policy(
            evaluations, combined_selector, latency_ms=100,
            valuation_mode="SETTLEMENT_WITH_REPRICING_CONFIRMATION", ideal=kind)
        upper[kind] = summarize(outcomes)
    result["idealized_upper_bounds"] = upper
    return result
