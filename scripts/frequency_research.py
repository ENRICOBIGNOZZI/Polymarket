#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = "polymarket_frequency_research_v1"


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def finite(value: Any) -> float | None:
    try:
        output = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return output if math.isfinite(output) else None


def leaves(value: Any, path: tuple[str, ...] = ()) -> Iterable[tuple[tuple[str, ...], Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield from leaves(child, path + (str(key),))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from leaves(child, path + (str(index),))
    else:
        yield path, value


def load_evidence(root: Path) -> tuple[list[tuple[str, Any]], int]:
    documents: list[tuple[str, Any]] = []
    jsonl_rows = 0
    if not root.exists():
        return documents, jsonl_rows

    for path in sorted(root.rglob("*.json")):
        payload = read_json(path, None)
        if payload is not None:
            documents.append((str(path.relative_to(root)), payload))

    for path in sorted(root.rglob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            documents.append((f"{path.relative_to(root)}:{index}", payload))
            jsonl_rows += 1
    return documents, jsonl_rows


def numbers(documents: list[tuple[str, Any]], aliases: set[str]) -> list[float]:
    output: list[float] = []
    normalized = {item.lower() for item in aliases}
    for _, payload in documents:
        for path, value in leaves(payload):
            if not path or path[-1].lower() not in normalized:
                continue
            number = finite(value)
            if number is not None:
                output.append(number)
    return output


def strings(documents: list[tuple[str, Any]], aliases: set[str]) -> list[str]:
    output: list[str] = []
    normalized = {item.lower() for item in aliases}
    for _, payload in documents:
        for path, value in leaves(payload):
            if path and path[-1].lower() in normalized and isinstance(value, str):
                output.append(value)
    return output


def max_or_zero(values: list[float]) -> float:
    return max(values) if values else 0.0


def sum_or_zero(values: list[float]) -> float:
    return sum(values) if values else 0.0


def safety_contract(repo_root: Path) -> tuple[dict[str, Any], list[str]]:
    champion = read_json(repo_root / "config" / "live_champion.json", {})
    alpha = read_json(repo_root / "config" / "alpha_factory.json", {})
    fast = read_json(repo_root / "config" / "fast_arb_policy.json", {})
    errors: list[str] = []

    if not champion:
        errors.append("missing explicit live champion manifest")
    if alpha.get("paper_only") is not True:
        errors.append("Alpha Factory paper_only must remain true")
    if alpha.get("allow_authenticated_execution") is not False:
        errors.append("authenticated execution must remain disabled")
    if alpha.get("allow_direct_champion_mutation") is not False:
        errors.append("direct champion mutation must remain disabled")
    if fast.get("mode") != "shadow":
        errors.append("fast-arbitrage policy must remain shadow-only")
    if fast.get("real_order_submission") is not False:
        errors.append("fast-arbitrage real order submission must remain false")

    contract = {
        "champion_version": champion.get("version"),
        "champion_loop": champion.get("loop"),
        "paper_only": alpha.get("paper_only") is True,
        "authenticated_execution": False,
        "direct_champion_mutation": False,
        "real_order_submission": False,
        "fast_policy_mode": fast.get("mode"),
    }
    return contract, errors


def component_status(repo_root: Path, mode: str) -> dict[str, Any]:
    expected = {
        "high": [
            "config/fast_arb_policy.json",
            "src/fast_arb.cpp",
            "src/fast_ws.cpp",
            "scripts/forward_maker_probe.py",
        ],
        "low": [
            "config/paper_v4.json",
            "src/pca_stat_arb.cpp",
            "src/stat_arb.cpp",
            "scripts/filter_coherent_hedges.py",
        ],
    }[mode]
    present = [path for path in expected if (repo_root / path).is_file()]
    missing = [path for path in expected if path not in present]
    return {"expected": expected, "present": present, "missing": missing}


def high_frequency_report(documents: list[tuple[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    text = json.dumps([payload for _, payload in documents], sort_keys=True).lower()
    stress_15 = any(token in text for token in ("1.5x", "stress_1_5", "stress15", "1.5"))
    stress_20 = any(token in text for token in ("2.0x", "stress_2_0", "stress20", "2.0"))

    metrics = {
        "ws_messages_max": max_or_zero(numbers(documents, {"ws_messages"})),
        "book_updates_max": max_or_zero(numbers(documents, {"book_updates"})),
        "eligible_markets_max": max_or_zero(numbers(documents, {"eligible_markets"})),
        "quote_definitions_max": max_or_zero(numbers(documents, {"quote_definitions"})),
        "pair_fill_rate_max": max_or_zero(numbers(documents, {"pair_fill_rate"})),
        "one_sided_fill_rate_max": max_or_zero(
            numbers(documents, {"one_sided_only_rate", "one_sided_fill_rate"})
        ),
        "maker_net_edge_max": max_or_zero(
            numbers(documents, {"maker_entry_net_edge", "maker_net_edge"})
        ),
        "taker_net_edge_max": max_or_zero(
            numbers(documents, {"taker_net_edge", "taker_entry_net_edge"})
        ),
        "conservative_pnl_ex_rewards_sum": sum_or_zero(
            numbers(documents, {"conservative_pnl_ex_rewards_usd"})
        ),
        "cost_stress_1_5x_present": stress_15,
        "cost_stress_2_0x_present": stress_20,
    }
    active_market_data = (
        metrics["ws_messages_max"] > 0
        or metrics["book_updates_max"] > 0
        or metrics["eligible_markets_max"] > 0
    )
    gates = {
        "evidence_present": bool(documents),
        "event_time_market_data_active": active_market_data,
        "explicit_1_5x_and_2_0x_cost_surface": stress_15 and stress_20,
        "promotion_authority": False,
    }
    backlog = [
        {
            "priority": 1,
            "candidate": "queue_fill_hazard",
            "hypothesis": "fill probability is predictable from queue proxy, depth, imbalance and latency",
            "required_evidence": "event-time order-book replay with partial fills and cancel/replace latency",
            "target_interface": "execution/fill expert",
        },
        {
            "priority": 2,
            "candidate": "microprice_adverse_selection",
            "hypothesis": "microprice and imbalance predict short-horizon mark movement after executable costs",
            "required_evidence": "maker/taker ablation under 1.0x, 1.5x and 2.0x costs",
            "target_interface": "microstructure fair-value expert",
        },
        {
            "priority": 3,
            "candidate": "latency_robust_fast_arbitrage",
            "hypothesis": "structural and conversion opportunities survive realistic detection-to-fill latency",
            "required_evidence": "timestamped websocket replay, stale-quote rejection and depth consumption",
            "target_interface": "fast-arbitrage intent generator",
        },
        {
            "priority": 4,
            "candidate": "maker_policy_controller",
            "hypothesis": "join/improve/fade policy selection can reduce one-sided fill and adverse-selection loss",
            "required_evidence": "forward shadow comparison with common markets and frozen capital limits",
            "target_interface": "execution policy expert",
        },
    ]
    return {"metrics": metrics, "gates": gates}, backlog


def low_frequency_report(documents: list[tuple[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    relations = [value.lower() for value in strings(documents, {"relation"})]
    text = json.dumps([payload for _, payload in documents], sort_keys=True).lower()
    metrics = {
        "coherent_maker_positive_max": max_or_zero(
            numbers(documents, {"coherent_maker_positive"})
        ),
        "coherent_raw_positive_max": max_or_zero(
            numbers(documents, {"coherent_raw_positive", "raw_positive"})
        ),
        "maker_net_edge_max": max_or_zero(
            numbers(documents, {"maker_entry_net_edge", "maker_net_edge"})
        ),
        "taker_net_edge_max": max_or_zero(
            numbers(documents, {"taker_net_edge", "taker_entry_net_edge"})
        ),
        "factor_observations_max": max_or_zero(numbers(documents, {"obs", "observations"})),
        "oos_trades_max": max_or_zero(numbers(documents, {"oos_trades", "trades"})),
        "oos_net_pnl_sum": sum_or_zero(numbers(documents, {"oos_net_pnl_usd", "net_pnl"})),
        "semantic_relations": sum(value == "semantic" for value in relations),
        "same_event_relations": sum(value == "same_event" for value in relations),
        "calibration_evidence_present": any(
            token in text for token in ("brier", "calibration", "reliability")
        ),
        "external_information_evidence_present": any(
            token in text for token in ("external_information", "external_probability", "external_signal")
        ),
    }
    gates = {
        "evidence_present": bool(documents),
        "executable_maker_edge_observed": metrics["coherent_maker_positive_max"] > 0
        or metrics["maker_net_edge_max"] > 0,
        "chronological_oos_evidence_present": metrics["oos_trades_max"] > 0,
        "calibration_evidence_present": metrics["calibration_evidence_present"],
        "promotion_authority": False,
    }
    backlog = [
        {
            "priority": 1,
            "candidate": "dynamic_logit_factor_model",
            "hypothesis": "time-varying factors and loadings improve stable cross-market fair values over rolling PCA",
            "required_evidence": "purged walk-forward Kalman/online-subspace comparison against current PCA",
            "target_interface": "cross-market fair-value expert",
        },
        {
            "priority": 2,
            "candidate": "graph_constrained_probability_projection",
            "hypothesis": "joint projection onto logical constraints creates executable relative value without inconsistent probabilities",
            "required_evidence": "complete event-set audit, fee/depth stress and graph ablation",
            "target_interface": "graph/logic expert",
        },
        {
            "priority": 3,
            "candidate": "learned_semantic_neighborhoods",
            "hypothesis": "learned event embeddings outperform hashed text while preserving conservative shrinkage",
            "required_evidence": "chronological neighbor retrieval and incremental utility versus graph/PCA experts",
            "target_interface": "semantic relative-value expert",
        },
        {
            "priority": 4,
            "candidate": "external_probability_adapters",
            "hypothesis": "timestamped outside forecasts add calibrated probability information beyond Polymarket prices",
            "required_evidence": "source-specific calibration, staleness decay and no-look-ahead provenance",
            "target_interface": "external-information expert",
        },
        {
            "priority": 5,
            "candidate": "hierarchical_calibration_and_joint_portfolio",
            "hypothesis": "global/category calibration plus joint covariance-aware allocation improves net utility",
            "required_evidence": "resolved-market Brier/log-loss and portfolio OOS under concentration/drawdown limits",
            "target_interface": "mixture-of-experts and portfolio/risk layer",
        },
    ]
    return {"metrics": metrics, "gates": gates}, backlog


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# {report['title']}",
        "",
        f"- generated_at: `{report['generated_at']}`",
        f"- mode: `{report['mode']}`",
        f"- decision: `{report['decision']}`",
        f"- evidence documents: {report['evidence']['documents']}",
        f"- JSONL rows: {report['evidence']['jsonl_rows']}",
        f"- paper-only: `{str(report['paper_only']).lower()}`",
        f"- promotion-ready: `{str(report['promotion_ready']).lower()}`",
        "",
        "## Gates",
    ]
    for key, value in report["analysis"]["gates"].items():
        lines.append(f"- {key}: `{str(value).lower()}`")
    lines.extend(["", "## Metrics", "```json"])
    lines.append(json.dumps(report["analysis"]["metrics"], indent=2, sort_keys=True))
    lines.extend(["```", "", "## Research backlog"])
    for item in report["research_backlog"]:
        lines.extend(
            [
                f"### {item['priority']}. `{item['candidate']}`",
                f"- hypothesis: {item['hypothesis']}",
                f"- required evidence: {item['required_evidence']}",
                f"- target interface: `{item['target_interface']}`",
            ]
        )
    lines.extend(
        [
            "",
            "## Safety boundary",
            "This scheduler produces research evidence only. It cannot merge, mutate the live champion, deploy, or submit orders.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build paper-only high/low-frequency model research reports")
    parser.add_argument("--mode", choices=("high", "low"), required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-markdown", required=True)
    parser.add_argument("--now")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    evidence_root = Path(args.evidence_root).resolve()
    contract, errors = safety_contract(repo_root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 2

    documents, jsonl_rows = load_evidence(evidence_root)
    analysis, backlog = (
        high_frequency_report(documents)
        if args.mode == "high"
        else low_frequency_report(documents)
    )
    evidence_ready = analysis["gates"]["evidence_present"]
    decision = "RESEARCH_CYCLE_COMPLETE" if evidence_ready else "MORE_EVIDENCE_REQUIRED"
    generated_at = args.now or datetime.now(timezone.utc).isoformat()
    title = (
        "Polymarket High-Frequency Model Research"
        if args.mode == "high"
        else "Polymarket Low-Frequency Model Research"
    )
    report = {
        "schema": SCHEMA,
        "title": title,
        "mode": args.mode,
        "generated_at": generated_at,
        "decision": decision,
        "paper_only": True,
        "real_order_submission": False,
        "authenticated_execution": False,
        "direct_champion_mutation": False,
        "promotion_ready": False,
        "integration_required": True,
        "safety_contract": contract,
        "components": component_status(repo_root, args.mode),
        "evidence": {
            "root": str(evidence_root),
            "documents": len(documents),
            "jsonl_rows": jsonl_rows,
            "sources": [source for source, _ in documents],
        },
        "analysis": analysis,
        "research_backlog": backlog,
    }

    output_json = Path(args.output_json)
    output_markdown = Path(args.output_markdown)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_markdown.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown = render_markdown(report)
    output_markdown.write_text(markdown, encoding="utf-8")
    print(markdown, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
