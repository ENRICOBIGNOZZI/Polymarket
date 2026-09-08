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
import math
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import research_v7_btc_m5_external_cancel_forward as forward
from v7_external_cancel_activation import evaluate_activation
from v7_crypto_execution_alpha import load_config as load_execution_alpha_config

EXPERIMENT_ID = "btc-m5-external-cancel-overlay-forward-v1"
PROTOCOL_SCHEMA = "polymarket_v7_btc_m5_external_cancel_episode_protocol_v3"
EPISODE_SCHEMA = "polymarket_v7_btc_m5_external_cancel_forward_episode_v3"
REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SCHEMA = "polymarket_v7_external_cancel_forward_runtime_v1"
BASELINE_MANIFEST_SCHEMA = "polymarket_v7_btc_m5_external_cancel_promotion_evidence_pack_v1"
OFFICIAL_PROTOCOL_SCHEMA = "polymarket_v7_btc_m5_external_cancel_episode_protocol_v3"
SEED_SCHEMA = "polymarket_v7_btc_m5_external_cancel_promotion_evidence_pack_v1"
SEED_RECEIPT_SCHEMA = "polymarket_v7_external_cancel_seed_import_receipt_v1"
SUMMARY_SCHEMA = "polymarket_v7_btc_m5_external_cancel_episode_build_summary_v3"
HASH64 = __import__("re").compile(r"^[0-9a-f]{64}$")
_SEED_VALIDATION_CACHE: dict[tuple[str, str, str], tuple[list[Path], dict[str, Any]]] = {}


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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _checked_hash(value: Any, field: str) -> str:
    raw = str(value or "")
    if not HASH64.fullmatch(raw):
        raise ValueError(f"seed_hash_invalid:{field}")
    return raw


def semantic_report_compare(
    expected: Any, actual: Any, *, path: str = "$",
    rel_tol: float = 1e-14, abs_tol: float = 1e-15,
) -> tuple[bool, float, list[str]]:
    """Compare deterministic evidence across Python/libm runtimes.

    Evidence files themselves remain byte/hash immutable.  Recomputed floating
    aggregates may differ by a few ulps across Python/libm implementations, so
    only finite non-boolean floats receive a machine-scale tolerance.  Types,
    keys, sequence lengths, integers, booleans, strings and nulls remain exact.
    """
    mismatches: list[str] = []
    max_abs_delta = 0.0

    def walk(left: Any, right: Any, where: str) -> None:
        nonlocal max_abs_delta
        if isinstance(left, bool) or isinstance(right, bool):
            if type(left) is not type(right) or left != right:
                mismatches.append(where)
            return
        numeric_left = isinstance(left, (int, float)) and not isinstance(left, bool)
        numeric_right = isinstance(right, (int, float)) and not isinstance(right, bool)
        if numeric_left or numeric_right:
            if not (numeric_left and numeric_right):
                mismatches.append(where); return
            # Integer-valued contract fields stay exact even though bool is a
            # subclass of int and has already been handled above.
            if isinstance(left, int) and isinstance(right, int):
                if left != right:
                    mismatches.append(where)
                return
            a, b = float(left), float(right)
            if not (math.isfinite(a) and math.isfinite(b)):
                if a != b:
                    mismatches.append(where)
                return
            delta = abs(a - b)
            max_abs_delta = max(max_abs_delta, delta)
            if not math.isclose(a, b, rel_tol=rel_tol, abs_tol=abs_tol):
                mismatches.append(where)
            return
        if isinstance(left, dict) or isinstance(right, dict):
            if not (isinstance(left, dict) and isinstance(right, dict)):
                mismatches.append(where); return
            if set(left) != set(right):
                mismatches.append(where + ".<keys>"); return
            for key in sorted(left):
                walk(left[key], right[key], f"{where}.{key}")
            return
        if isinstance(left, list) or isinstance(right, list):
            if not (isinstance(left, list) and isinstance(right, list)) or len(left) != len(right):
                mismatches.append(where); return
            for index, (a, b) in enumerate(zip(left, right)):
                walk(a, b, f"{where}[{index}]")
            return
        if type(left) is not type(right) or left != right:
            mismatches.append(where)

    walk(expected, actual, path)
    return not mismatches, max_abs_delta, mismatches[:20]


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_seed_path(raw: Any, *, repository_root: Path) -> Path:
    """Resolve an immutable evidence path, with safe relocation under repo/runs."""
    text = str(raw or "")
    if not text:
        raise ValueError("seed_path_empty")
    repo = repository_root.resolve()
    runs = (repo / "runs").resolve()
    candidate = Path(text).expanduser()
    if candidate.is_absolute() and candidate.is_file():
        resolved = candidate.resolve()
    else:
        parts = candidate.parts
        if "runs" in parts:
            index = parts.index("runs")
            resolved = (repo / Path(*parts[index:])).resolve()
        else:
            resolved = (repo / candidate).resolve()
    if not _under(resolved, runs) or not resolved.is_file():
        raise ValueError(f"seed_path_unavailable_or_outside_runs:{text}")
    return resolved


def _verify_ref(value: Any, *, repository_root: Path, field: str, path_key: str = "path") -> tuple[Path, str]:
    if not isinstance(value, dict) or path_key not in value or "sha256" not in value:
        raise ValueError(f"seed_reference_shape:{field}")
    path = resolve_seed_path(value[path_key], repository_root=repository_root)
    expected = _checked_hash(value["sha256"], f"{field}:sha256")
    actual = sha256_file(path)
    if actual != expected:
        raise ValueError(f"seed_hash_mismatch:{field}")
    return path, expected


def _protocol_contract(value: dict[str, Any], exp: dict[str, Any]) -> int:
    """Validate the immutable semantic core while permitting richer provenance."""
    rule = exp["frozen_rule"]
    original_boundary = freeze_ms(exp)
    promotion_boundary = value.get("promotion_evidence_market_started_strictly_after_ms")
    development = value.get("development_semantics") if isinstance(value.get("development_semantics"), dict) else {}
    trigger = value.get("trigger_protocol") if isinstance(value.get("trigger_protocol"), dict) else {}
    incumbent = value.get("incumbent_proxy") if isinstance(value.get("incumbent_proxy"), dict) else {}
    overlay = value.get("overlay") if isinstance(value.get("overlay"), dict) else {}
    stress = value.get("stress") if isinstance(value.get("stress"), dict) else {}
    labels = value.get("labels") if isinstance(value.get("labels"), dict) else {}
    minimum = value.get("minimum_evidence") if isinstance(value.get("minimum_evidence"), dict) else {}
    registry_minimum = exp.get("minimum_evidence") if isinstance(exp.get("minimum_evidence"), dict) else {}
    if (
        value.get("schema") != PROTOCOL_SCHEMA
        or value.get("episode_schema") != EPISODE_SCHEMA
        or value.get("canonical_rule_sha256") != sha256_json(rule)
        or value.get("canonical_rule") != rule
        or isinstance(promotion_boundary, bool) or not isinstance(promotion_boundary, int)
        or promotion_boundary < original_boundary
        or int(development.get("overlap_warmup_ms") or 0) != 300
        or int(development.get("overlap_tail_ms") or 0) != 1200
        or int(trigger.get("grid_ms") or 0) != 25
        or int(trigger.get("shock_window_ms") or rule["shock_window_ms"]) != 100
        or abs(float(trigger.get("minimum_absolute_log_return_bp", rule["minimum_absolute_log_return_bp"])) - 0.3) > 1e-12
        or int(trigger.get("cooldown_ms") or rule["trigger_cooldown_ms"]) != 250
        or abs(float(incumbent.get("quote_size_shares") or 0.0) - float(registry_minimum.get("quote_size_shares") or 5.0)) > 1e-12
        or int(incumbent.get("primary_fill_window_ms") or 0) != 500
        or abs(float(incumbent.get("queue_ahead_multiplier", rule["queue_ahead_multiplier"])) - 1.5) > 1e-12
        or int(overlay.get("effective_cancel_latency_ms") or 0) != 100
        or abs(float(stress.get("queue_ahead_multiplier") or 0.0) - 3.0) > 1e-12
        or int(stress.get("effective_cancel_latency_ms") or 0) != 200
        or labels.get("horizons_ms") != [250, 500, 1000]
    ):
        raise ValueError("seed_protocol_contract_drift")
    if minimum and minimum != registry_minimum:
        raise ValueError("seed_protocol_minimum_evidence_drift")
    if value.get("experiment_id") not in (None, EXPERIMENT_ID):
        raise ValueError("seed_protocol_experiment_identity")
    if value.get("freeze_merge_sha") not in (None, exp.get("freeze_merge_sha")):
        raise ValueError("seed_protocol_freeze_sha")
    if value.get("freeze_merge_timestamp_utc") not in (None, exp.get("freeze_merge_timestamp_utc")):
        raise ValueError("seed_protocol_freeze_timestamp")
    if value.get("frozen_at_ms") is not None and int(value["frozen_at_ms"]) != promotion_boundary:
        raise ValueError("seed_protocol_promotion_boundary_identity")
    authority = value.get("authority")
    if authority is not None:
        if (not isinstance(authority, dict) or authority.get("paper_only") is not True
            or authority.get("authenticated_execution") is not False
            or authority.get("real_order_submission") is not False
            or authority.get("real_capital_at_risk") is not False
            or authority.get("automatic_promotion") is not False
            or authority.get("execution_authority") != "RESEARCH_ZERO_AUTHORITY"):
            raise ValueError("seed_protocol_authority")
    if value.get("predecessor_promotion_credit") not in (None, False):
        raise ValueError("seed_protocol_predecessor_credit")
    maker_sha = str(value.get("maker_model_sha") or "")
    maker_published = value.get("maker_model_published_ms")
    if (len(maker_sha) != 40 or any(ch not in "0123456789abcdef" for ch in maker_sha)
        or isinstance(maker_published, bool) or not isinstance(maker_published, int) or maker_published <= 0):
        raise ValueError("seed_protocol_maker_identity")
    producer_sha = value.get("producer_evaluator_main_sha")
    if producer_sha is not None and (not isinstance(producer_sha, str) or len(producer_sha) != 40
        or any(ch not in "0123456789abcdef" for ch in producer_sha)):
        raise ValueError("seed_protocol_producer_identity")
    return promotion_boundary


def import_seed_evidence(
    *, seed_manifest: Path | None, registry: Path, research_root: Path,
    repository_root: Path,
) -> tuple[list[Path], dict[str, Any]]:
    """Verify, copy and re-evaluate a frozen promotion evidence pack.

    The aggregate PASS is never trusted. Every referenced immutable artifact is
    hash-checked, each episode summary is tied back to its causal inputs, then
    the current evaluator recomputes the report from the copied episode JSONL.
    """
    if seed_manifest is None or not seed_manifest.is_file():
        return [], {"state": "NO_SEED", "seed_episode_files": 0}
    repository_root = repository_root.resolve()
    seed_manifest = seed_manifest.resolve()
    if not _under(seed_manifest, (repository_root / "runs").resolve()):
        raise ValueError("seed_manifest_outside_repository_runs")
    exp = experiment(registry)
    manifest = load(seed_manifest)
    expected_fields = {
        "schema", "paper_only", "authenticated_execution", "real_order_submission",
        "automatic_promotion", "market_count", "episode_files", "external_segments",
        "protocol", "report",
    }
    if (set(manifest) != expected_fields or manifest.get("schema") != SEED_SCHEMA
        or manifest.get("paper_only") is not True
        or manifest.get("authenticated_execution") is not False
        or manifest.get("real_order_submission") is not False
        or manifest.get("automatic_promotion") is not False):
        raise ValueError("seed_manifest_safety_or_shape")
    manifest_sha = sha256_file(seed_manifest)
    cache_key = (str(seed_manifest.resolve()), manifest_sha, str(research_root.resolve()))
    cached = _SEED_VALIDATION_CACHE.get(cache_key)
    if cached is not None and all(path.is_file() for path in cached[0]):
        return list(cached[0]), {**cached[1], "validation_cached": True}
    protocol_path, protocol_sha = _verify_ref(
        manifest["protocol"], repository_root=repository_root, field="protocol")
    stored_report_path, stored_report_sha = _verify_ref(
        manifest["report"], repository_root=repository_root, field="report")
    protocol = load(protocol_path)
    promotion_boundary = _protocol_contract(protocol, exp)

    external_rows = manifest.get("external_segments")
    if not isinstance(external_rows, list) or not external_rows:
        raise ValueError("seed_external_segments_missing")
    external_sources: dict[str, str] = {}
    for index, row in enumerate(external_rows):
        if not isinstance(row, dict) or set(row) != {"copy", "source", "sha256"}:
            raise ValueError(f"seed_external_shape:{index}")
        copy_path, digest = _verify_ref(
            row, repository_root=repository_root, field=f"external:{index}", path_key="copy")
        source = str(row.get("source") or "")
        if not source or source in external_sources or not copy_path.name:
            raise ValueError(f"seed_external_identity:{index}")
        external_sources[source] = digest

    episode_rows = manifest.get("episode_files")
    market_count = manifest.get("market_count")
    if (not isinstance(episode_rows, list) or not episode_rows
        or isinstance(market_count, bool) or not isinstance(market_count, int)
        or market_count != len(episode_rows)):
        raise ValueError("seed_episode_market_count")

    target = research_root / "seed" / manifest_sha
    target_episodes = target / "episodes"
    target_summaries = target / "summaries"
    target_episodes.mkdir(parents=True, exist_ok=True)
    target_summaries.mkdir(parents=True, exist_ok=True)
    copied_paths: list[Path] = []
    seen_markets: set[str] = set()
    verified_book_sources: dict[str, str] = {}
    tape_dump_hashes: set[str] = set()
    for index, row in enumerate(episode_rows):
        if not isinstance(row, dict) or set(row) != {
            "episodes", "episodes_sha256", "summary", "summary_sha256"
        }:
            raise ValueError(f"seed_episode_shape:{index}")
        episode_path = resolve_seed_path(row["episodes"], repository_root=repository_root)
        episode_sha = _checked_hash(row["episodes_sha256"], f"episode:{index}")
        if sha256_file(episode_path) != episode_sha:
            raise ValueError(f"seed_hash_mismatch:episode:{index}")
        summary_path = resolve_seed_path(row["summary"], repository_root=repository_root)
        summary_sha = _checked_hash(row["summary_sha256"], f"summary:{index}")
        if sha256_file(summary_path) != summary_sha:
            raise ValueError(f"seed_hash_mismatch:summary:{index}")
        summary = load(summary_path)
        market = str(summary.get("market_id") or "")
        if (summary.get("schema") != SUMMARY_SCHEMA or not market or market in seen_markets
            or summary.get("market_evaluable") is not True
            or summary.get("evidence_valid") is not True
            or summary.get("promotion_eligible_market") is not True
            or int(summary.get("promotion_boundary_ms") or 0) != promotion_boundary
            or int(summary.get("market_started_ms") or 0) <= promotion_boundary
            or summary.get("exclusion_reason_codes") != []
            or summary.get("causality_violations") != []
            or summary.get("rule_sha256") != sha256_json(exp["frozen_rule"])
            or summary.get("protocol_sha256") != protocol_sha
            or summary.get("output_sha256") != episode_sha):
            raise ValueError(f"seed_summary_contract:{index}")
        seen_markets.add(market)
        provenance = summary.get("source_provenance") if isinstance(summary.get("source_provenance"), dict) else {}
        source_manifest = resolve_seed_path(summary.get("manifest"), repository_root=repository_root)
        if sha256_file(source_manifest) != _checked_hash(provenance.get("manifest_sha256"), f"book_manifest:{market}"):
            raise ValueError(f"seed_book_manifest_hash:{market}")
        books = provenance.get("book_segments")
        ext_refs = provenance.get("external_segments")
        if not isinstance(books, list) or not books or not isinstance(ext_refs, list) or not ext_refs:
            raise ValueError(f"seed_source_provenance_missing:{market}")
        for source in books:
            if not isinstance(source, dict) or set(source) != {"path", "sha256"}:
                raise ValueError(f"seed_book_source_shape:{market}")
            source_path = resolve_seed_path(source["path"], repository_root=repository_root)
            digest = _checked_hash(source["sha256"], f"book:{market}")
            key = str(source_path)
            if key not in verified_book_sources:
                if sha256_file(source_path) != digest:
                    raise ValueError(f"seed_book_source_hash:{market}")
                verified_book_sources[key] = digest
            elif verified_book_sources[key] != digest:
                raise ValueError(f"seed_book_source_hash_conflict:{market}")
        for source in ext_refs:
            if not isinstance(source, dict) or set(source) != {"path", "sha256"}:
                raise ValueError(f"seed_external_provenance_shape:{market}")
            source_name = str(source["path"] or "")
            digest = _checked_hash(source["sha256"], f"external_provenance:{market}")
            if external_sources.get(source_name) != digest:
                raise ValueError(f"seed_external_provenance_mismatch:{market}")
        tape_dump_hashes.add(_checked_hash(provenance.get("tape_dump_sha256"), f"tape_dump:{market}"))

        copied_episode = target_episodes / f"{market}.jsonl"
        copied_summary = target_summaries / f"{market}.json"
        if not copied_episode.exists() or sha256_file(copied_episode) != episode_sha:
            shutil.copy2(episode_path, copied_episode)
        if not copied_summary.exists() or sha256_file(copied_summary) != summary_sha:
            shutil.copy2(summary_path, copied_summary)
        if sha256_file(copied_episode) != episode_sha or sha256_file(copied_summary) != summary_sha:
            raise ValueError(f"seed_copy_hash_mismatch:{market}")
        copied_paths.append(copied_episode)

    if len(seen_markets) != market_count or len(tape_dump_hashes) != 1:
        raise ValueError("seed_market_or_decoder_identity")
    protocol_decoder_hash = protocol.get("tape_decoder_binary_sha256")
    if protocol_decoder_hash is not None and _checked_hash(protocol_decoder_hash, "protocol_decoder") not in tape_dump_hashes:
        raise ValueError("seed_decoder_hash_drift")
    stored_report = load(stored_report_path)
    recomputed = forward.evaluate(registry, sorted(copied_paths), experiment_id=EXPERIMENT_ID)
    semantic_match, max_abs_numeric_delta, mismatch_paths = semantic_report_compare(stored_report, recomputed)
    if not semantic_match:
        raise ValueError("seed_report_recompute_mismatch:" + ",".join(mismatch_paths))
    activation = evaluate_activation(recomputed)
    if recomputed.get("state") != "PASS" or activation.get("paper_execution_alpha_overlay_eligible") is not True:
        raise ValueError("seed_report_does_not_pass_frozen_gate")
    receipt = {
        "schema": SEED_RECEIPT_SCHEMA, "paper_only": True,
        "authenticated_execution": False, "real_order_submission": False,
        "real_money_authority": False, "automatic_promotion": False,
        "execution_authority": "RESEARCH_ZERO_AUTHORITY",
        "seed_manifest_sha256": manifest_sha, "protocol_sha256": protocol_sha,
        "stored_report_sha256": stored_report_sha, "recomputed_report_sha256": sha256_json(recomputed),
        "report_exact_json_match": recomputed == stored_report,
        "report_semantic_match": semantic_match,
        "report_max_abs_numeric_delta": max_abs_numeric_delta,
        "report_numeric_rel_tol": 1e-14, "report_numeric_abs_tol": 1e-15,
        "rule_sha256": sha256_json(exp["frozen_rule"]),
        "original_freeze_boundary_ms": freeze_ms(exp), "promotion_boundary_ms": promotion_boundary,
        "market_count": market_count,
        "episode_file_count": len(copied_paths), "external_segment_count": len(external_rows),
        "verified_book_source_count": len(verified_book_sources),
        "tape_dump_sha256": next(iter(tape_dump_hashes)), "forward_state": recomputed["state"],
        "activation_eligible": True,
    }
    atomic_json(target / "validation_receipt.json", receipt)
    for path in [*copied_paths, *target_summaries.glob("*.json")]:
        try:
            path.chmod(0o444)
        except OSError:
            pass
    result = {"state": "VERIFIED_PASS", "validation_cached": False, **receipt}
    copied_paths = sorted(copied_paths)
    _SEED_VALIDATION_CACHE[cache_key] = (list(copied_paths), dict(result))
    return copied_paths, result

def official_v3_evidence(config_path: Path) -> dict[str, Any]:
    config = load_execution_alpha_config(config_path)
    cancel = config["execution_alpha"]["cancel"]
    value = cancel.get("official_v3_evidence")
    if not isinstance(value, dict):
        raise ValueError("official_v3_evidence_missing")
    return value


def verify_immutable_baseline(
    *, report_path: Path | None, manifest_path: Path | None, protocol_path: Path | None,
    pins: dict[str, Any], exp: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    paths = (report_path, manifest_path, protocol_path)
    present = [path is not None and path.is_file() for path in paths]
    if not any(present):
        return None, {"available": False, "verified": False}
    if not all(present):
        raise ValueError("official_v3_baseline_partial")
    assert report_path is not None and manifest_path is not None and protocol_path is not None
    report_sha = sha256_file(report_path)
    manifest_sha = sha256_file(manifest_path)
    protocol_sha = sha256_file(protocol_path)
    if (
        report_sha != pins["baseline_report_sha256"]
        or manifest_sha != pins["baseline_manifest_sha256"]
        or protocol_sha != pins["protocol_sha256"]
    ):
        raise ValueError("official_v3_baseline_hash_mismatch")
    manifest = load(manifest_path)
    protocol = load(protocol_path)
    report = load(report_path)
    if (
        manifest.get("schema") != BASELINE_MANIFEST_SCHEMA
        or manifest.get("paper_only") is not True
        or manifest.get("authenticated_execution") is not False
        or manifest.get("real_order_submission") is not False
        or manifest.get("automatic_promotion") is not False
        or int(manifest.get("market_count") or 0) < int(pins["minimum_independent_markets"])
        or (manifest.get("protocol") or {}).get("sha256") != protocol_sha
        or (manifest.get("report") or {}).get("sha256") != report_sha
    ):
        raise ValueError("official_v3_baseline_manifest_invalid")
    trigger = protocol.get("trigger_protocol") if isinstance(protocol.get("trigger_protocol"), dict) else {}
    development = protocol.get("development_semantics") if isinstance(protocol.get("development_semantics"), dict) else {}
    overlay = protocol.get("overlay") if isinstance(protocol.get("overlay"), dict) else {}
    stress = protocol.get("stress") if isinstance(protocol.get("stress"), dict) else {}
    if (
        protocol.get("schema") != OFFICIAL_PROTOCOL_SCHEMA
        or protocol.get("canonical_rule_sha256") != pins["rule_sha256"]
        or protocol.get("canonical_rule") != exp["frozen_rule"]
        or int(protocol.get("promotion_evidence_market_started_strictly_after_ms") or 0)
            != int(pins["promotion_boundary_ms"])
        or int(trigger.get("grid_ms") or 0) != 25
        or int(trigger.get("shock_window_ms") or 0) != 100
        or int(trigger.get("cooldown_ms") or 0) != 250
        or abs(float(trigger.get("minimum_absolute_log_return_bp") or 0.0) - 0.3) > 1e-12
        or int(development.get("overlap_warmup_ms") or 0) != 300
        or int(development.get("overlap_tail_ms") or 0) != 1200
        or int(overlay.get("effective_cancel_latency_ms") or 0) != 100
        or overlay.get("cancel_only_stale_side") is not True
        or int(stress.get("effective_cancel_latency_ms") or 0) != 200
        or abs(float(stress.get("queue_ahead_multiplier") or 0.0) - 3.0) > 1e-12
    ):
        raise ValueError("official_v3_baseline_protocol_invalid")
    activation = evaluate_activation(report)
    if activation.get("paper_execution_alpha_overlay_eligible") is not True:
        raise ValueError("official_v3_baseline_report_not_eligible")
    return report, {
        "available": True, "verified": True, "report_sha256": report_sha,
        "manifest_sha256": manifest_sha, "protocol_sha256": protocol_sha,
        "promotion_boundary_ms": int(pins["promotion_boundary_ms"]),
        "market_count": int(report.get("market_count") or 0),
        "episode_count": int(report.get("episode_count") or 0),
        "avoidable_fill_events": int(report.get("avoidable_fill_events") or 0),
    }


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
    promotion_boundary_ms: int | None = None,
) -> dict[str, Any]:
    rule = exp["frozen_rule"]
    minimum = exp.get("minimum_evidence") if isinstance(exp.get("minimum_evidence"), dict) else {}
    return {
        "schema": PROTOCOL_SCHEMA,
        "episode_schema": EPISODE_SCHEMA,
        "canonical_rule_sha256": sha256_json(rule),
        "canonical_rule": rule,
        "promotion_evidence_market_started_strictly_after_ms": int(
            promotion_boundary_ms if promotion_boundary_ms is not None else freeze_ms(exp)
        ),
        "maker_model_published_ms": int(maker_model_published_ms),
        "maker_model_sha": maker_model_sha,
        "development_semantics": {
            "overlap_warmup_ms": 300, "overlap_tail_ms": 1200,
            "state_operator": "latest valid receive-time observation at or before target",
            "no_future_snapshot_requirement": True,
        },
        "trigger_protocol": {
            "grid_ms": 25,
            "shock_window_ms": int(rule["shock_window_ms"]),
            "cooldown_ms": int(rule["trigger_cooldown_ms"]),
            "minimum_absolute_log_return_bp": float(rule["minimum_absolute_log_return_bp"]),
            "confirmation": "Coinbase 100ms midpoint return is zero or same sign as Binance",
        },
        "incumbent_proxy": {
            "quote_size_shares": float(minimum.get("quote_size_shares") or 5.0),
            "primary_fill_window_ms": 500,
        },
        "overlay": {
            "effective_cancel_latency_ms": int(rule["cancel_latency_ms"]),
            "cancel_only_stale_side": True, "overlay_may_not_create_fill": True,
        },
        "stress": {
            "name": "queue_3x_cancel_200ms",
            "queue_ahead_multiplier": 3.0, "effective_cancel_latency_ms": 200,
        },
        "labels": {
            "horizons_ms": [250, 500, 1000], "anchor": "trigger_receive_time",
        },
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
        rows.extend(sorted(root.glob(f"{prefix}*.bin.gz")))
    return sorted(set(path for path in rows if not path.name.endswith(".open")))


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
    model_sha: str, execution_alpha_config: Path = REPO_ROOT / "config/v7_crypto_execution_alpha.json",
    baseline_report: Path | None = None, baseline_manifest: Path | None = None,
    baseline_protocol: Path | None = None, seed_manifest: Path | None = None,
    repository_root: Path | None = None,
) -> dict[str, Any]:
    exp = experiment(registry)
    pins = official_v3_evidence(execution_alpha_config)
    if sha256_json(exp["frozen_rule"]) != pins["rule_sha256"]:
        raise ValueError("official_v3_rule_registry_mismatch")
    if exp.get("freeze_merge_sha") != pins["freeze_merge_sha"]:
        raise ValueError("official_v3_freeze_registry_mismatch")
    maker_sha, published_ms = model_identity(run_root, model_sha)
    protocol = build_protocol(
        exp, maker_model_sha=maker_sha, maker_model_published_ms=published_ms,
        promotion_boundary_ms=int(pins["promotion_boundary_ms"]),
    )
    protocol_path = research_root / "protocol" / f"{model_sha}.json"
    if protocol_path.exists():
        if load(protocol_path) != protocol:
            raise ValueError("external_cancel_protocol_identity_changed_after_freeze")
    else:
        atomic_json(protocol_path, protocol)

    baseline, baseline_provenance = verify_immutable_baseline(
        report_path=baseline_report, manifest_path=baseline_manifest,
        protocol_path=baseline_protocol, pins=pins, exp=exp,
    )
    seed_episode_paths, seed_status = import_seed_evidence(
        seed_manifest=seed_manifest, registry=registry, research_root=research_root,
        repository_root=(repository_root or REPO_ROOT),
    )
    seed_verified = (
        seed_status.get("state") == "VERIFIED_PASS"
        and seed_status.get("seed_manifest_sha256") == pins["baseline_manifest_sha256"]
        and seed_status.get("protocol_sha256") == pins["protocol_sha256"]
        and seed_status.get("stored_report_sha256") == pins["baseline_report_sha256"]
        and seed_status.get("rule_sha256") == pins["rule_sha256"]
        and int(seed_status.get("promotion_boundary_ms") or 0) == int(pins["promotion_boundary_ms"])
    ) if seed_manifest is not None else True
    if seed_manifest is not None and not seed_verified:
        raise ValueError("official_v3_seed_provenance_mismatch")

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
            session = load(manifest)
            started_ms = int(session.get("started_ms") or 0)
            # Closed observer sessions carry their actual start receive time.
            # A missing start is not silently invented; producer validation remains authoritative.
            if started_ms and started_ms <= published_ms:
                failed[market] = "SESSION_STARTED_BEFORE_FROZEN_MAKER_PUBLICATION"
                continue
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
            command[0] = __import__("sys").executable
            command.insert(1, str(Path(__file__).with_name("research_v7_btc_m5_external_cancel_episode_build.py")))
            try:
                subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                for episode in forward.iter_jsonl([output]):
                    forward.validate_episode(episode, freeze_ms=freeze_ms(exp),
                        expected_rule_hash=protocol["canonical_rule_sha256"],
                        quote_size=float(protocol["incumbent_proxy"]["quote_size_shares"]), book_schema=2)
                processed += 1
            except (subprocess.CalledProcessError, forward.EvidenceError) as exc:
                failed[market] = (getattr(exc, "stderr", None) or str(exc))[-500:]
                output.unlink(missing_ok=True)
                summary.unlink(missing_ok=True)

    current_episode_paths = sorted(episodes_root.glob("*.jsonl"))
    episode_paths = sorted(seed_episode_paths + current_episode_paths)
    current_report = forward.evaluate(registry, episode_paths, experiment_id=EXPERIMENT_ID)
    current_report_path = research_root / "forward_report.json"
    atomic_json(current_report_path, current_report)
    current_activation = evaluate_activation(current_report)
    current_eligible = current_activation["paper_execution_alpha_overlay_eligible"] is True

    activation_report = current_report
    activation_source = "CURRENT_FORWARD"
    if baseline is not None and (
        not current_eligible
        or int(baseline.get("market_count") or 0) >= int(current_report.get("market_count") or 0)
    ):
        activation_report = baseline
        activation_source = "IMMUTABLE_OFFICIAL_V3_BASELINE"

    selected_report_path = research_root / "activation_forward_report.json"
    atomic_json(selected_report_path, activation_report)
    activation = evaluate_activation(activation_report)
    activation_report_sha = (
        str(baseline_provenance.get("report_sha256"))
        if activation_source == "IMMUTABLE_OFFICIAL_V3_BASELINE"
        else sha256_file(selected_report_path)
    )
    provenance_verified = (
        baseline_provenance.get("verified") is True and seed_verified
        if activation_source == "IMMUTABLE_OFFICIAL_V3_BASELINE"
        else seed_verified
            and protocol["canonical_rule_sha256"] == pins["rule_sha256"]
            and int(protocol["promotion_evidence_market_started_strictly_after_ms"])
                == int(pins["promotion_boundary_ms"])
    )
    activation["evidence"].update({
        "official_v3_provenance_verified": bool(provenance_verified),
        "activation_source": activation_source,
        "official_v3_promotion_boundary_ms": int(pins["promotion_boundary_ms"]),
        "official_v3_protocol_reference_sha256": pins["protocol_sha256"],
        "activation_report_sha256": activation_report_sha,
        "baseline_evidence_manifest_sha256": (
            baseline_provenance.get("manifest_sha256") if baseline_provenance.get("verified") else None
        ),
        "seed_evidence_recomputed": bool(seed_verified and seed_manifest is not None),
        "seed_validation_receipt_sha256": (
            sha256_file(research_root / "seed" / seed_status["seed_manifest_sha256"] / "validation_receipt.json")
            if seed_verified and seed_manifest is not None else None
        ),
    })
    if not provenance_verified:
        activation["paper_execution_alpha_overlay_eligible"] = False
        activation["failed_checks"] = sorted(set(
            list(activation.get("failed_checks") or []) + ["official_v3_provenance_verified"]
        ))
    activation_path = run_root / "control" / "external_cancel_activation.json"
    atomic_json(activation_path, activation)
    status = {
        "schema": RUNTIME_SCHEMA, "paper_only": True,
        "authenticated_execution": False, "real_order_submission": False,
        "real_money_authority": False, "automatic_promotion": False,
        "execution_authority": "RESEARCH_ZERO_AUTHORITY",
        "model_sha": model_sha, "rule_sha256": protocol["canonical_rule_sha256"],
        "protocol_path": str(protocol_path), "book_root": str(book_root),
        "official_v3_promotion_boundary_ms": int(pins["promotion_boundary_ms"]),
        "closed_market_sessions": len(sessions), "newly_processed_markets": processed,
        "current_episode_files": len(current_episode_paths),
        "seed_episode_files": len(seed_episode_paths), "durable_episode_files": len(episode_paths),
        "seed_evidence": seed_status, "processing_failures": failed,
        "external_closed_segments": len(ext), "forward_state": current_report["state"],
        "market_count": current_report["market_count"],
        "avoidable_fill_events": current_report["avoidable_fill_events"],
        "activation_source": activation_source,
        "activation_market_count": int(activation_report.get("market_count") or 0),
        "baseline_forward_verified": baseline_provenance.get("verified") is True,
        "baseline_provenance": baseline_provenance,
        "activation_eligible": activation["paper_execution_alpha_overlay_eligible"],
        "activation_path": str(activation_path),
        "forward_report_path": str(current_report_path),
        "activation_report_path": str(selected_report_path),
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
    parser.add_argument("--execution-alpha-config", type=Path, default=REPO_ROOT / "config/v7_crypto_execution_alpha.json")
    parser.add_argument("--baseline-report", type=Path)
    parser.add_argument("--baseline-manifest", type=Path)
    parser.add_argument("--baseline-protocol", type=Path)
    parser.add_argument("--seed-manifest", type=Path)
    parser.add_argument("--repository-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()
    while True:
        try:
            status = evaluate_once(
                run_root=args.run_root, research_root=args.research_root,
                registry=args.registry, tape_dump=args.tape_dump, model_sha=args.model_sha,
                execution_alpha_config=args.execution_alpha_config,
                baseline_report=args.baseline_report, baseline_manifest=args.baseline_manifest,
                baseline_protocol=args.baseline_protocol, seed_manifest=args.seed_manifest,
                repository_root=args.repository_root,
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
