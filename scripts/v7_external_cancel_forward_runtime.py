#!/usr/bin/env python3
"""Durable zero-authority runner for the frozen BTC M5 external-cancel study.

It converts only closed receive-time tapes into forward episodes, accumulates
those episodes across exact-SHA PAPER runs, recomputes the immutable forward
report, and independently materializes the PAPER activation gate.  It never
submits/cancels an order and never retunes the frozen rule.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import research_v7_btc_m5_external_cancel_forward as forward
from v7_external_cancel_activation import evaluate_activation

EXPERIMENT_ID = "btc-m5-external-cancel-overlay-forward-v1"
PROTOCOL_SCHEMA = "polymarket_v7_btc_m5_external_cancel_episode_protocol_v3"
EPISODE_SCHEMA = "polymarket_v7_btc_m5_external_cancel_forward_episode_v3"
RUNTIME_SCHEMA = "polymarket_v7_external_cancel_forward_runtime_v1"


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)




def fail_closed_activation(error: str) -> dict[str, Any]:
    return {
        "schema": "polymarket_v7_external_cancel_activation_v1",
        "experiment_id": EXPERIMENT_ID,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_money_authority": False,
        "automatic_promotion": False,
        "paper_execution_alpha_overlay_eligible": False,
        "manual_exact_sha_promotion_required": True,
        "frozen_rule_retuning_allowed": False,
        "forward_state": "RUNTIME_FAIL_CLOSED",
        "checks": {},
        "failed_checks": [error],
        "forward_reason_codes": [],
        "evidence": {},
    }

def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def sha256_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def freeze_ms(experiment: dict[str, Any]) -> int:
    raw = str(experiment.get("start_time") or "")
    if not raw.endswith("Z"):
        raise ValueError("freeze_timestamp_invalid")
    return int(datetime.fromisoformat(raw[:-1] + "+00:00").timestamp() * 1000)


def experiment(registry_path: Path) -> dict[str, Any]:
    registry = load(registry_path)
    rows = registry.get("experiments") if isinstance(registry.get("experiments"), list) else []
    row = next((item for item in rows if isinstance(item, dict) and item.get("experiment_id") == EXPERIMENT_ID), None)
    if row is None:
        raise ValueError("external_cancel_experiment_missing")
    rule = row.get("frozen_rule") if isinstance(row.get("frozen_rule"), dict) else {}
    expected = {
        "shock_source": "BINANCE_SPOT_TRADES", "shock_window_ms": 100,
        "minimum_absolute_log_return_bp": 0.3,
        "confirmation_source": "COINBASE_SPOT_TOP_OF_BOOK", "confirmation": "NON_OPPOSING",
        "trigger_cooldown_ms": 250, "baseline_action": "JOIN",
        "queue_ahead_multiplier": 1.5, "cancel_latency_ms": 100,
        "retuning_after_freeze": False,
    }
    if any(rule.get(key) != value for key, value in expected.items()):
        raise ValueError("external_cancel_frozen_rule_drift")
    if row.get("execution_authority") != "RESEARCH_ZERO_AUTHORITY" or row.get("promotion_credit") is not False:
        raise ValueError("external_cancel_experiment_authority")
    return row


def build_protocol(
    exp: dict[str, Any], *, maker_model_sha: str, maker_model_published_ms: int,
) -> dict[str, Any]:
    rule = exp["frozen_rule"]
    minimum = exp.get("minimum_evidence") if isinstance(exp.get("minimum_evidence"), dict) else {}
    return {
        "schema": PROTOCOL_SCHEMA,
        "episode_schema": EPISODE_SCHEMA,
        "canonical_rule_sha256": sha256_json(rule),
        "canonical_rule": rule,
        "promotion_evidence_market_started_strictly_after_ms": freeze_ms(exp),
        "maker_model_published_ms": int(maker_model_published_ms),
        "maker_model_sha": maker_model_sha,
        "development_semantics": {"overlap_warmup_ms": 300, "overlap_tail_ms": 1200},
        "trigger_protocol": {"grid_ms": 25},
        "incumbent_proxy": {
            "quote_size_shares": float(minimum.get("quote_size_shares") or 5.0),
            "primary_fill_window_ms": 500,
        },
        "overlay": {"effective_cancel_latency_ms": int(rule["cancel_latency_ms"])},
        "stress": {"queue_ahead_multiplier": 3.0, "effective_cancel_latency_ms": 200},
        "labels": {"horizons_ms": [250, 500, 1000]},
    }


def model_identity(run_root: Path, model_sha: str) -> tuple[str, int]:
    value = load(run_root / "micro_maker" / "execution_model.json")
    generated = int(value.get("generated_ts_ms") or 0)
    if (
        value.get("schema") != "polymarket_v7_maker_execution_model_v1"
        or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("model_sha") != model_sha
        or generated <= 0
    ):
        raise ValueError("maker_champion_identity_not_ready")
    return model_sha, generated


def external_segments(run_root: Path) -> list[Path]:
    root = run_root / "external_fair" / "normalized_events"
    rows: list[Path] = []
    for prefix in ("binance-spot", "coinbase-spot"):
        rows.extend(sorted(root.glob(f"{prefix}*.bin")))
    return sorted(set(rows))


def closed_sessions(book_root: Path) -> dict[str, tuple[Path, list[Path]]]:
    """Choose one complete observer session per market, preferring most evidence."""
    chosen: dict[str, tuple[Path, list[Path]]] = {}
    for manifest in sorted(book_root.glob("btc-m5-book.*.*.manifest.json")):
        value = load(manifest)
        market = str(value.get("market_id") or "")
        if not market or int(value.get("payload_schema_version") or 0) != 2:
            continue
        stem = manifest.name[:-len(".manifest.json")]
        open_segments = list((book_root / "normalized_events").glob(f"{stem}*.bin.open"))
        segments = sorted((book_root / "normalized_events").glob(f"{stem}*.bin"))
        if open_segments or not segments:
            continue
        incumbent = chosen.get(market)
        if incumbent is None or len(segments) > len(incumbent[1]):
            chosen[market] = (manifest, segments)
    return chosen


def market_workspace(root: Path, market: str, manifest: Path, segments: list[Path]) -> Path:
    workspace = root / "workspaces" / market
    if workspace.exists():
        shutil.rmtree(workspace)
    (workspace / "normalized_events").mkdir(parents=True)
    shutil.copy2(manifest, workspace / manifest.name)
    for source in segments:
        target = workspace / "normalized_events" / source.name
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)
    return workspace


def evaluate_once(
    *, run_root: Path, research_root: Path, registry: Path, tape_dump: Path,
    model_sha: str,
) -> dict[str, Any]:
    exp = experiment(registry)
    maker_sha, published_ms = model_identity(run_root, model_sha)
    protocol = build_protocol(exp, maker_model_sha=maker_sha, maker_model_published_ms=published_ms)
    protocol_path = research_root / "protocol" / f"{model_sha}.json"
    atomic_json(protocol_path, protocol)

    book_root = research_root / "books" / model_sha
    episodes_root = research_root / "episodes"
    summaries_root = research_root / "summaries"
    episodes_root.mkdir(parents=True, exist_ok=True)
    summaries_root.mkdir(parents=True, exist_ok=True)
    ext = external_segments(run_root)
    sessions = closed_sessions(book_root)
    processed = 0
    failed: dict[str, str] = {}
    if ext and tape_dump.is_file():
        for market, (manifest, segments) in sessions.items():
            output = episodes_root / f"{market}.{model_sha}.jsonl"
            summary = summaries_root / f"{market}.{model_sha}.json"
            if output.exists() and summary.exists():
                continue
            workspace = market_workspace(research_root, market, manifest, segments)
            command = [
                str(tape_dump), "--root", str(workspace), "--market", market,
                "--protocol", str(protocol_path), "--external-tape", *map(str, ext),
                "--tape-dump", str(tape_dump.parent / "polymarket_v7_research_external_cancel_tape_dump"),
                "--output", str(output), "--summary", str(summary),
            ]
            # The builder is Python; tape_dump here is the builder path supplied by launcher.
            command[0] = __import__("sys").executable
            command.insert(1, str(Path(__file__).with_name("research_v7_btc_m5_external_cancel_episode_build.py")))
            try:
                subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                processed += 1
            except subprocess.CalledProcessError as exc:
                failed[market] = (exc.stderr or f"exit:{exc.returncode}")[-500:]
                output.unlink(missing_ok=True)
                summary.unlink(missing_ok=True)

    episode_paths = sorted(episodes_root.glob("*.jsonl"))
    report = forward.evaluate(registry, episode_paths, experiment_id=EXPERIMENT_ID)
    report_path = research_root / "forward_report.json"
    atomic_json(report_path, report)
    activation = evaluate_activation(report)
    activation_path = run_root / "control" / "external_cancel_activation.json"
    atomic_json(activation_path, activation)
    status = {
        "schema": RUNTIME_SCHEMA, "paper_only": True,
        "authenticated_execution": False, "real_order_submission": False,
        "real_money_authority": False, "automatic_promotion": False,
        "execution_authority": "RESEARCH_ZERO_AUTHORITY",
        "model_sha": model_sha, "rule_sha256": protocol["canonical_rule_sha256"],
        "protocol_path": str(protocol_path), "book_root": str(book_root),
        "closed_market_sessions": len(sessions), "newly_processed_markets": processed,
        "durable_episode_files": len(episode_paths), "processing_failures": failed,
        "external_closed_segments": len(ext), "forward_state": report["state"],
        "market_count": report["market_count"], "avoidable_fill_events": report["avoidable_fill_events"],
        "activation_eligible": activation["paper_execution_alpha_overlay_eligible"],
        "activation_path": str(activation_path), "forward_report_path": str(report_path),
    }
    atomic_json(research_root / "runtime_status.json", status)
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--research-root", type=Path, required=True)
    parser.add_argument("--registry", type=Path, default=Path("config/v7_maker_fillability_experiments.json"))
    parser.add_argument("--tape-dump", type=Path, required=True,
                        help="built polymarket_v7_research_external_cancel_tape_dump executable")
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()
    while True:
        try:
            status = evaluate_once(
                run_root=args.run_root, research_root=args.research_root,
                registry=args.registry, tape_dump=args.tape_dump, model_sha=args.model_sha,
            )
            print(json.dumps(status, sort_keys=True), flush=True)
        except Exception as exc:
            error = f"FAIL_CLOSED:{type(exc).__name__}:{exc}"
            status = {
                "schema": RUNTIME_SCHEMA, "paper_only": True,
                "authenticated_execution": False, "real_order_submission": False,
                "real_money_authority": False, "automatic_promotion": False,
                "execution_authority": "RESEARCH_ZERO_AUTHORITY", "model_sha": args.model_sha,
                "state": "FAIL_CLOSED", "error": error,
            }
            atomic_json(
                args.run_root / "control" / "external_cancel_activation.json",
                fail_closed_activation(error),
            )
            atomic_json(args.research_root / "runtime_status.json", status)
            print(json.dumps(status, sort_keys=True), flush=True)
        if not args.loop:
            return 0
        time.sleep(max(5.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
