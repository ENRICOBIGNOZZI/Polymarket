"""Public-safe immutable V2 report receipts."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

from .core import SAFETY, SCHEMA, atomic_json, digest


def git_sha(root):
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


def audit_receipt(root):
    return {
        "schema": SCHEMA + "_audit_v1", **SAFETY, "code_sha": git_sha(root),
        "differences": [
            {"object": "training_target", "v1": "settlement outcome", "v2": "settlement and actual PM repricing",
             "evidence": "research/learning/models.py; research/walk_forward_v2/core.py:book_targets"},
            {"object": "prediction_target", "v1": "one log-loss selected family", "v2": "each family receives OOS economics",
             "evidence": "research/backtest/fit.py; research/walk_forward_v2/core.py:walk_forward"},
            {"object": "validation_objective", "v1": "settlement log loss", "v2": "prediction metrics and economic replay separated",
             "evidence": "research/backtest/fit.py; research/walk_forward_v2/core.py:economic_evaluation"},
            {"object": "trading_decision", "v1": "forecast minus ask/fee/reserve", "v2": "same gates plus direct repricing and funnel",
             "evidence": "research/backtest/run.py:replay; research/walk_forward_v2/core.py:replay_one"},
            {"object": "execution", "v1": "first post-latency L1 book", "v2": "same first-observed, same-epoch censoring",
             "evidence": "research/backtest/run.py:Tape.after; research/walk_forward_v2/core.py:arrival"},
            {"object": "final_pnl", "v1": "settlement only", "v2": "settlement PnL and 250ms markout separate",
             "evidence": "research/backtest/run.py:metrics; research/walk_forward_v2/core.py:summarize"},
        ],
        "label_provenance": {
            "ARCHIVED_CAUSAL_RECEIVE_TIME": "promotion quality",
            "RETROSPECTIVE_REPORTED_RESOLUTION_TIME": "exploratory only",
            "UNAVAILABLE": "excluded, never zero",
        },
        "pm_baseline_diagnostic": {
            "method": "OOS forecast - executable ask; then fee and execution reserve; before all policy filters",
            "status": "PENDING_OOS_INPUT",
        },
    }


def manifest(data):
    rows = data["decisions"]
    observed = sum(v.get("state") == "OBSERVED" for row in rows for v in row.get("targets", {}).values())
    return {
        "schema": SCHEMA + "_data_manifest_v1", **SAFETY, "data_sha256": data["data_sha256"],
        "input_state": data["input_state"], "minimum_wall_ns": data["minimum_wall_ns"],
        "immutable_source_objects": len(data["sources"]), "raw_source_records": None,
        "deduplicated_decisions": len(rows), "unique_markets": len({r["market_id"] for r in rows}),
        "assets": sorted({r["asset"] for r in rows}), "horizons": sorted({r["horizon"] for r in rows}),
        "labeled": sum(r["label"] is not None for r in rows),
        "archived_causal_labels": sum(r["label_provenance"] == "ARCHIVED_CAUSAL_RECEIVE_TIME" for r in rows),
        "retrospective_labels": sum(r["label_provenance"] == "RETROSPECTIVE_REPORTED_RESOLUTION_TIME" for r in rows),
        "unresolved": sum(r["label"] is None for r in rows),
        "short_horizon_observed_pairs": observed, "sources": data["sources"], "exclusions": data["exclusions"],
    }


def method():
    return {
        "schema": SCHEMA + "_methodology_v1", **SAFETY, "clock": "INTEGER_NANOSECONDS_ONLY",
        "folds": "WHOLE_MARKET_CHRONOLOGICAL_EXPANDING_WITH_2S_EMBARGO",
        "target": "FIRST_OBSERVED_SAME_EPOCH_PM_BOOK_AT_OR_AFTER_TARGET_WITHIN_50MS; NO_INTERPOLATION_OR_FORWARD_FILL",
        "target_horizons_ms": [25, 50, 100, 250, 500, 1000, 2000],
        "execution": "L1_VISIBLE_DEPTH_PARTIAL_FILL; FIRST_POST_LATENCY_BOOK_WITHIN_50MS; MISSING_IS_CENSORED",
        "latencies_ms": [10, 25, 50, 100, 250, 500],
        "primary_latency": "LATENCY_NOT_EMPIRICALLY_IDENTIFIED",
        "uncertainty": "MARKET_BLOCK_BOOTSTRAP_IF_AT_LEAST_FOUR_MARKETS",
    }


def chart(output, state):
    names = [
        "01-walk-forward-cumulative-pnl", "02-pnl-by-model", "03-pnl-by-latency",
        "04-edge-decay-vs-latency", "05-predicted-edge-vs-markout",
        "06-predicted-edge-vs-settlement-pnl", "07-funnel-survival",
        "08-pm-baseline-executable-edge", "09-model-logloss-comparison",
        "10-repricing-prediction-quality", "11-pnl-by-asset",
        "12-pnl-by-contract-horizon", "13-friction-decomposition", "14-fill-rate-vs-predicted-edge",
    ]
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return {"state": "MATPLOTLIB_UNAVAILABLE", "files": []}
    files = []
    for name in names:
        fig, axis = plt.subplots(figsize=(8, 4))
        axis.set_title(name.replace("-", " "))
        axis.text(.5, .5, "UNAVAILABLE: " + state if state != "READY" else "See JSON receipt",
                  ha="center", va="center", transform=axis.transAxes)
        axis.set_xticks([]); axis.set_yticks([])
        fig.tight_layout()
        path = Path(output) / (name + ".png")
        fig.savefig(path, dpi=130)
        plt.close(fig)
        files.append(path.name)
    return {"state": "READY", "files": files}


def publish(output, *, root, start_sha, data, folds, economics):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    audit, data_manifest, methodology = audit_receipt(root), manifest(data), method()
    model_metrics = {"schema": SCHEMA + "_model_metrics_v1", **SAFETY,
                     "state": "OOS_READY" if folds["oos_predictions"] else "NO_OOS_PREDICTIONS",
                     "families": ["pm", "logistic_offset", "boosted_offset", "repricing_ridge"]}
    funnel = {key: value["metrics"]["funnel"] for key, value in economics.get("models", {}).items()}
    parity = {"schema": SCHEMA + "_execution_parity_v1", **SAFETY, "rows": [
        {"rule": "post-latency book", "live": "required", "backtest": "required", "same": True},
        {"rule": "visible L1 partial fill", "live": "required", "backtest": "required", "same": True},
        {"rule": "fee, tick, entry cap", "live": "native terms", "backtest": "native terms", "same": True},
        {"rule": "global capital allocator", "live": "global", "backtest": "sequential research reservation", "same": False},
    ]}
    values = {
        "audit.json": audit, "methodology.json": methodology, "data_manifest.json": data_manifest,
        "walk_forward_folds.json": folds, "model_metrics.json": model_metrics,
        "economic_metrics.json": economics,
        "funnel.json": {"schema": SCHEMA + "_funnel_v1", **SAFETY, "models": funnel},
        "execution_parity.json": parity,
        "latency_analysis.json": {"schema": SCHEMA + "_latency_v1", **SAFETY, "primary": methodology["primary_latency"], "fixed": economics.get("latency", {})},
        "uncertainty.json": {"schema": SCHEMA + "_uncertainty_v1", **SAFETY,
                               "models": {key: value["uncertainty"] for key, value in economics.get("models", {}).items()}},
    }
    values["results.json"] = {"schema": SCHEMA + "_results_v1", **SAFETY, "start_sha": start_sha,
                              "audit_sha256": digest(audit), "data_sha256": data_manifest["data_sha256"],
                              "fold_sha256": digest(folds), "economic_sha256": digest(economics)}
    for name, value in values.items():
        atomic_json(output / name, value)
    charts = chart(output, data_manifest["input_state"])
    direct = "UNKNOWN / INSUFFICIENT EVIDENCE" if data_manifest["input_state"] != "READY" else "SEE_RESULTS_JSON"
    lines = [
        "# HISTORICAL WALK-FORWARD V2", "", "## Direct answers", "",
        "- Positive gross alpha: **" + direct + "**",
        "- Positive net executable alpha: **" + direct + "**",
        "- PM lag after causal external signal: **" + direct + "**",
        "- Prior zero-trade diagnosis: **OOS PM executable-edge distribution is in economic_metrics.json.**",
        "- Principal bottleneck: **" + ("ADMISSIBLE_HFT_INPUT_UNAVAILABLE" if data_manifest["input_state"] != "READY" else "SEE_FUNNEL") + "**",
        "", "## Identity", "",
        "- Starting SHA: " + start_sha,
        "- Data SHA: " + data_manifest["data_sha256"],
        "- Input state: " + data_manifest["input_state"],
        "- Decisions: " + str(data_manifest["deduplicated_decisions"]),
        "- Markets: " + str(data_manifest["unique_markets"]),
        "- Short-horizon pairs: " + str(data_manifest["short_horizon_observed_pairs"]),
        "", "The report never converts unavailable books, labels, forecasts, or fills into zero.",
    ]
    for name in charts["files"]:
        lines.extend(["", "![" + name + "](" + name + ")"])
    report = output / "REPORT.md"
    temporary = report.with_suffix(".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(report)
    return values
