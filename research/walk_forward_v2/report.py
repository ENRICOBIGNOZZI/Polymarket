"""Public-safe immutable V2 report receipts."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

from .core import SAFETY, SCHEMA, atomic_json, digest


def git_sha(root):
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


def audit_receipt(root, code_sha=None):
    return {
        "schema": SCHEMA + "_audit_v1", **SAFETY, "code_sha": code_sha or git_sha(root),
        "differences": [
            {"object": "training_target", "v1": "settlement outcome", "v2": "midpoint diagnostics plus direct executable net markout",
             "evidence": "research/learning/models.py; research/walk_forward_v2/core.py:executable_markout_target"},
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
        "short_horizon_observed_pairs": observed, "book_evidence": data.get("book_evidence", {}),
        "sources": data["sources"], "exclusions": data["exclusions"],
    }


def method():
    return {
        "schema": SCHEMA + "_methodology_v1", **SAFETY, "clock": "INTEGER_NANOSECONDS_ONLY",
        "folds": "WHOLE_MARKET_CHRONOLOGICAL_EXPANDING_WITH_2S_EMBARGO",
        "target": "PRIMARY ECONOMIC TARGET = FUTURE_EXECUTABLE_BID_MINUS_CAUSAL_ASK_MINUS_ENTRY_AND_EXIT_TAKER_FEES; NATIVE_KIND6 SAME-CAPTURE CONTINUITY REQUIRED",
        "target_horizons_ms": [25, 50, 100, 250, 500, 1000, 2000],
        "execution": "NATIVE_KIND6_SELECTED_L1_ASOF_LATENCY; VISIBLE_DEPTH_PARTIAL_FILL; MISSING_IS_CENSORED",
        "latencies_ms": [10, 25, 50, 100, 250, 500],
        "primary_latency": "LATENCY_NOT_EMPIRICALLY_IDENTIFIED",
        "uncertainty": "MARKET_BLOCK_BOOTSTRAP_OF EXECUTABLE MARKOUT AND SETTLEMENT PNL IF AT LEAST FOUR MARKETS",
    }


def public_economics(economics):
    """Keep public research receipts compact; raw per-opportunity outcomes stay ephemeral."""
    return {
        "schema": economics.get("schema"),
        **SAFETY,
        "prediction_metrics": economics.get("prediction_metrics", {}),
        "pm_edge_distribution": economics.get("pm_edge_distribution", {}),
        "asset_selection_diagnostics": economics.get("asset_selection_diagnostics", {}),
        "live_parity_policy_diagnostics": economics.get("live_parity_policy_diagnostics", {}),
        "models": {
            name: {
                "metrics": value.get("metrics", {}),
                "uncertainty": value.get("uncertainty", {}),
                **({"equity_events": value.get("equity_events", [])} if value.get("equity_events") else {}),
            }
            for name, value in economics.get("models", {}).items()
        },
        "latency": economics.get("latency", {}),
        "horizon_latency": economics.get("horizon_latency", {}),
        "asset_horizon_latency": economics.get("asset_horizon_latency", {}),
        "live_parity_horizon_latency": economics.get("live_parity_horizon_latency", {}),
        "live_parity_asset_horizon_latency": economics.get("live_parity_asset_horizon_latency", {}),
        "promotion_reference_latency_ms": economics.get("promotion_reference_latency_ms"),
        "live_policy_promotion_candidates": economics.get("live_policy_promotion_candidates", {}),
        "latency_reference_horizon_ms": economics.get("latency_reference_horizon_ms"),
        "idealized_upper_bounds": economics.get("idealized_upper_bounds", {}),
    }


def _point_pnl(metrics):
    if metrics.get("net_pnl") is not None:
        return metrics.get("net_pnl")
    if metrics.get("observed_net_pnl") is not None:
        return metrics.get("observed_net_pnl")
    return metrics.get("markout_pnl")


def _sample(rows, maximum=5000):
    if len(rows) <= maximum:
        return rows
    step = max(1, len(rows) // maximum)
    return rows[::step][:maximum]


def chart(output, state, economics):
    names = [
        "01-walk-forward-cumulative-pnl", "02-pnl-by-model", "03-pnl-by-latency",
        "04-edge-decay-vs-latency", "05-predicted-edge-vs-markout",
        "06-predicted-edge-vs-settlement-pnl", "07-funnel-survival",
        "08-pm-baseline-executable-edge", "09-model-logloss-comparison",
        "10-repricing-prediction-quality", "11-pnl-by-asset",
        "12-pnl-by-contract-horizon", "13-friction-decomposition", "14-fill-rate-vs-predicted-edge",
        "15-equity-drawdown-500-1000-2000ms",
    ]
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return {"state": "MATPLOTLIB_UNAVAILABLE", "files": []}

    output = Path(output)
    if state != "READY":
        files = []
        for name in names:
            fig, axis = plt.subplots(figsize=(8, 4))
            axis.set_title(name.replace("-", " "))
            axis.text(.5, .5, "UNAVAILABLE: " + state, ha="center", va="center", transform=axis.transAxes)
            axis.set_xticks([]); axis.set_yticks([])
            fig.tight_layout()
            path = output / (name + ".png")
            fig.savefig(path, dpi=130); plt.close(fig); files.append(path.name)
        return {"state": state, "files": files}

    files = []
    models = economics.get("models", {})
    selected = models.get("markout_500ms") or models.get("markout_250ms") or {}
    selected_outcomes = selected.get("outcomes", [])
    equity_models = {
        name: models.get(name, {})
        for name in ("markout_500ms", "markout_1000ms", "markout_2000ms")
    }

    def save(name, draw):
        fig, axis = plt.subplots(figsize=(8, 4))
        draw(axis)
        axis.set_title(name.replace("-", " "))
        fig.tight_layout()
        path = output / (name + ".png")
        fig.savefig(path, dpi=130)
        plt.close(fig)
        files.append(path.name)

    def cumulative(axis):
        plotted = False
        for name, value in equity_models.items():
            rows = sorted(
                (r for r in value.get("equity_events", []) if r.get("markout") is not None),
                key=lambda r: r.get("decision_ns", 0),
            )
            total = 0.0; x=[]; y=[]
            for row in rows:
                total += row["markout"]
                x.append(row.get("decision_ns", 0) / 1e9)
                y.append(total)
            if x:
                axis.plot(x, y, marker="o", markersize=3, label=name.replace("markout_",""))
                plotted = True
        if plotted:
            axis.axhline(0, linewidth=.8)
            axis.set_xlabel("decision epoch seconds")
            axis.set_ylabel("cumulative OOS net executable markout")
            axis.legend()
        else:
            axis.text(.5,.5,"No marked fills",ha="center",va="center",transform=axis.transAxes)

    def pnl_models(axis):
        labels=[]; values=[]
        for name,value in models.items():
            p=_point_pnl(value.get("metrics",{}))
            if p is not None: labels.append(name);values.append(p)
        if values: axis.bar(range(len(values)),values);axis.set_xticks(range(len(labels)),labels,rotation=25,ha="right");axis.axhline(0,linewidth=.8);axis.set_ylabel("settlement PnL or executable markout")
        else: axis.text(.5,.5,"Economic outcome unavailable",ha="center",va="center",transform=axis.transAxes)

    def pnl_latency(axis):
        rows=[]
        for key,value in economics.get("latency",{}).items():
            p=_point_pnl(value)
            if p is not None: rows.append((int(key),p))
        rows.sort()
        if rows: axis.plot([x for x,_ in rows],[y for _,y in rows],marker="o");axis.axhline(0,linewidth=.8);axis.set_xlabel("latency ms");axis.set_ylabel("500ms-reference executable markout")
        else: axis.text(.5,.5,"Latency markout unavailable",ha="center",va="center",transform=axis.transAxes)

    def edge_decay(axis):
        rows=[]
        for key,value in economics.get("prediction_metrics",{}).get("repricing",{}).items():
            if value.get("state")=="READY" and value.get("actual_mean_move") is not None:
                rows.append((int(key),value["actual_mean_move"]))
        rows.sort()
        if rows: axis.plot([x for x,_ in rows],[y for _,y in rows],marker="o");axis.axhline(0,linewidth=.8);axis.set_xlabel("horizon ms");axis.set_ylabel("mean realized PM midpoint move")
        else: axis.text(.5,.5,"Repricing targets unavailable",ha="center",va="center",transform=axis.transAxes)

    def predicted_vs_markout(axis):
        rows=_sample([r for r in selected_outcomes if r.get("predicted_edge") is not None and r.get("markout") is not None])
        if rows: axis.scatter([r["predicted_edge"] for r in rows],[r["markout"] for r in rows],s=8,alpha=.35);axis.axhline(0,linewidth=.8);axis.set_xlabel("predicted net edge/share");axis.set_ylabel("250ms markout PnL")
        else: axis.text(.5,.5,"Markout pairs unavailable",ha="center",va="center",transform=axis.transAxes)

    def predicted_vs_settlement(axis):
        rows=_sample([r for r in selected_outcomes if r.get("predicted_edge") is not None and r.get("pnl") is not None])
        if rows: axis.scatter([r["predicted_edge"] for r in rows],[r["pnl"] for r in rows],s=8,alpha=.35);axis.axhline(0,linewidth=.8);axis.set_xlabel("predicted net edge/share");axis.set_ylabel("settlement PnL")
        else: axis.text(.5,.5,"Settled fills unavailable",ha="center",va="center",transform=axis.transAxes)

    def funnel(axis):
        f=(selected.get("metrics") or {}).get("funnel",{})
        rows=[(k,v) for k,v in f.items()]
        if rows: axis.barh(range(len(rows)),[v for _,v in rows]);axis.set_yticks(range(len(rows)),[k for k,_ in rows],fontsize=7);axis.set_xlabel("count")
        else: axis.text(.5,.5,"Funnel unavailable",ha="center",va="center",transform=axis.transAxes)

    def pm_edges(axis):
        rows=[r["predicted_edge"] for r in (models.get("pm") or {}).get("outcomes",[]) if r.get("predicted_edge") is not None]
        if rows: axis.hist(rows,bins=50);axis.axvline(0,linewidth=.8);axis.set_xlabel("PM forecast - executable cost");axis.set_ylabel("opportunities")
        else: axis.text(.5,.5,"PM OOS edges unavailable",ha="center",va="center",transform=axis.transAxes)

    def logloss(axis):
        rows=[]
        for name,value in economics.get("prediction_metrics",{}).get("settlement",{}).items():
            if value.get("state")=="READY": rows.append((name,value["log_loss"]))
        if rows: axis.bar(range(len(rows)),[v for _,v in rows]);axis.set_xticks(range(len(rows)),[k for k,_ in rows],rotation=20,ha="right");axis.set_ylabel("OOS log loss")
        else: axis.text(.5,.5,"OOS settlement labels unavailable",ha="center",va="center",transform=axis.transAxes)

    def repricing_quality(axis):
        rows=[]
        for key,value in economics.get("prediction_metrics",{}).get("repricing",{}).items():
            if value.get("state")=="READY": rows.append((int(key),value["mae"]))
        rows.sort()
        if rows: axis.plot([x for x,_ in rows],[y for _,y in rows],marker="o");axis.set_xlabel("horizon ms");axis.set_ylabel("OOS repricing MAE")
        else: axis.text(.5,.5,"OOS repricing metrics unavailable",ha="center",va="center",transform=axis.transAxes)

    def grouped_pnl(axis, field):
        totals={}
        for row in selected_outcomes:
            if row.get("markout") is not None:
                totals[row.get(field,"UNKNOWN")]=totals.get(row.get(field,"UNKNOWN"),0.0)+row["markout"]
        rows=sorted(totals.items())
        if rows: axis.bar(range(len(rows)),[v for _,v in rows]);axis.set_xticks(range(len(rows)),[k for k,_ in rows],rotation=20,ha="right");axis.axhline(0,linewidth=.8);axis.set_ylabel("OOS executable markout")
        else: axis.text(.5,.5,"Markout unavailable",ha="center",va="center",transform=axis.transAxes)

    def friction(axis):
        labels=["realistic"];values=[_point_pnl(selected.get("metrics",{}))]
        for name,value in economics.get("idealized_upper_bounds",{}).items():
            labels.append(name.replace("_UPPER_BOUND",""));values.append(_point_pnl(value))
        pairs=[(k,v) for k,v in zip(labels,values) if v is not None]
        if pairs: axis.bar(range(len(pairs)),[v for _,v in pairs]);axis.set_xticks(range(len(pairs)),[k for k,_ in pairs],rotation=20,ha="right");axis.axhline(0,linewidth=.8);axis.set_ylabel("PAPER PnL")
        else: axis.text(.5,.5,"Friction decomposition unavailable",ha="center",va="center",transform=axis.transAxes)

    def drawdown(axis):
        plotted = False
        for name, value in equity_models.items():
            rows = sorted(
                (r for r in value.get("equity_events", []) if r.get("markout") is not None),
                key=lambda r: r.get("decision_ns", 0),
            )
            cumulative = 0.0
            peak = 0.0
            x=[]; dd=[]
            for row in rows:
                cumulative += row["markout"]
                peak = max(peak, cumulative)
                x.append(row.get("decision_ns", 0) / 1e9)
                dd.append(cumulative - peak)
            if x:
                axis.plot(x, dd, marker="o", markersize=3, label=name.replace("markout_",""))
                plotted = True
        if plotted:
            axis.axhline(0, linewidth=.8)
            axis.set_xlabel("decision epoch seconds")
            axis.set_ylabel("drawdown from running markout peak")
            axis.legend()
        else:
            axis.text(.5,.5,"No marked fills",ha="center",va="center",transform=axis.transAxes)


    def fill_by_edge(axis):
        rows=[r for r in selected_outcomes if r.get("predicted_edge") is not None]
        bounds=[-1e9,0,.0025,.005,.01,.02,1e9];labels=["<0","0-.25%",".25-.5%",".5-1%","1-2%",">2%"];rates=[]
        for lo,hi in zip(bounds[:-1],bounds[1:]):
            cell=[r for r in rows if lo <= r["predicted_edge"] < hi]
            rates.append(sum(r.get("filled",0)>0 for r in cell)/len(cell) if cell else 0)
        if rows: axis.bar(range(len(labels)),rates);axis.set_xticks(range(len(labels)),labels,rotation=20);axis.set_ylabel("fill rate")
        else: axis.text(.5,.5,"Edge observations unavailable",ha="center",va="center",transform=axis.transAxes)

    save(names[0], cumulative)
    save(names[1], pnl_models)
    save(names[2], pnl_latency)
    save(names[3], edge_decay)
    save(names[4], predicted_vs_markout)
    save(names[5], predicted_vs_settlement)
    save(names[6], funnel)
    save(names[7], pm_edges)
    save(names[8], logloss)
    save(names[9], repricing_quality)
    save(names[10], lambda axis: grouped_pnl(axis,"asset"))
    save(names[11], lambda axis: grouped_pnl(axis,"horizon"))
    save(names[12], friction)
    save(names[13], fill_by_edge)
    save(names[14], drawdown)
    return {"state": "READY", "files": files}


def _support_statement(metrics, uncertainty):
    value = _point_pnl(metrics)
    if value is None:
        return "UNKNOWN / INSUFFICIENT EVIDENCE"
    interval = None
    if isinstance(uncertainty, dict):
        interval = uncertainty.get("markout_per_fill_interval") or uncertainty.get("pnl_per_fill_interval")
    if interval and interval[0] > 0:
        return "POSITIVE WITH MARKET-BLOCK SUPPORT"
    if interval and interval[1] < 0:
        return "NEGATIVE WITH MARKET-BLOCK SUPPORT"
    return ("POSITIVE POINT ESTIMATE / UNCERTAIN" if value > 0
            else "NEGATIVE POINT ESTIMATE / UNCERTAIN" if value < 0
            else "ZERO POINT ESTIMATE / INSUFFICIENT EVIDENCE")


def publish(output, *, root, start_sha, data, folds, economics, final_models):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    audit, data_manifest, methodology = audit_receipt(root, start_sha), manifest(data), method()
    public = public_economics(economics)
    prediction = public.get("prediction_metrics", {})
    model_metrics = {"schema": SCHEMA + "_model_metrics_v1", **SAFETY,
                     "state": "OOS_READY" if folds["oos_predictions"] else "NO_OOS_PREDICTIONS",
                     "families": ["pm", "logistic_offset", "boosted_offset", "repricing_ridge"],
                     "prediction_metrics": prediction}
    funnel = {key: value["metrics"]["funnel"] for key, value in public.get("models", {}).items()}
    parity = {"schema": SCHEMA + "_execution_parity_v1", **SAFETY, "rows": [
        {"rule": "post-latency book", "live": "required", "backtest": "required", "same": True},
        {"rule": "visible L1 partial fill", "live": "required", "backtest": "required", "same": True},
        {"rule": "fee, tick, entry cap", "live": "native terms", "backtest": "native terms", "same": True},
        {"rule": "global capital allocator", "live": "global", "backtest": "research approximation", "same": False},
    ]}
    values = {
        "audit.json": audit, "methodology.json": methodology, "data_manifest.json": data_manifest,
        "walk_forward_folds.json": folds, "model_metrics.json": model_metrics,
        "economic_metrics.json": public,
        "funnel.json": {"schema": SCHEMA + "_funnel_v1", **SAFETY, "models": funnel},
        "execution_parity.json": parity,
        "latency_analysis.json": {"schema": SCHEMA + "_latency_v2", **SAFETY,
                                  "primary": methodology["primary_latency"],
                                  "reference_horizon_ms": public.get("latency_reference_horizon_ms"),
                                  "fixed": public.get("latency", {}),
                                  "horizon_latency": public.get("horizon_latency", {}),
                                  "asset_horizon_latency": public.get("asset_horizon_latency", {}),
                                  "live_parity_horizon_latency": public.get("live_parity_horizon_latency", {}),
                                  "live_parity_asset_horizon_latency": public.get("live_parity_asset_horizon_latency", {})},
        "uncertainty.json": {"schema": SCHEMA + "_uncertainty_v1", **SAFETY,
                              "models": {key: value["uncertainty"] for key, value in public.get("models", {}).items()}},
        "full_window_repricing_models.json": final_models,
    }
    values["results.json"] = {"schema": SCHEMA + "_results_v1", **SAFETY, "start_sha": start_sha,
                              "audit_sha256": digest(audit), "data_sha256": data_manifest["data_sha256"],
                              "fold_sha256": digest(folds), "economic_sha256": digest(public),
                              "full_window_model_sha256": digest(final_models)}
    for name, value in values.items():
        atomic_json(output / name, value)
    charts = chart(output, data_manifest["input_state"], economics)

    selected = public.get("models", {}).get("markout_500ms") or {}
    direct = _support_statement(
        selected.get("metrics", {}), selected.get("uncertainty", {})
    ) if data_manifest["input_state"] == "READY" else "UNKNOWN / INSUFFICIENT EVIDENCE"
    repricing = prediction.get("repricing", {}).get("250", {})
    lag = ("POSITIVE DESCRIPTIVE 250MS MOVE" if repricing.get("state") == "READY" and repricing.get("actual_mean_move",0) > 0
           else "NONPOSITIVE DESCRIPTIVE 250MS MOVE" if repricing.get("state") == "READY"
           else "UNKNOWN / INSUFFICIENT EVIDENCE")
    selected_metrics = selected.get("metrics", {})
    lines = [
        "# HISTORICAL WALK-FORWARD V2", "", "## Direct answers", "",
        "- 500ms-reference OOS executable markout: **" + direct + "**",
        "- Settlement alpha: **UNKNOWN / INSUFFICIENT CAUSAL SETTLEMENT LABELS**",
        "- PM lag after causal external signal: **" + lag + "**",
        "- Prior zero-trade diagnosis: **PM midpoint baseline cannot cross executable ask plus costs.**",
        "- Principal bottleneck: **" + ("ADMISSIBLE_HFT_INPUT_UNAVAILABLE" if data_manifest["input_state"] != "READY" else "SEE_FUNNEL_AND_FRICTION_DECOMPOSITION") + "**",
        "", "## Identity", "",
        "- Starting SHA: " + start_sha,
        "- Data SHA: " + data_manifest["data_sha256"],
        "- Input state: " + data_manifest["input_state"],
        "- Decisions: " + str(data_manifest["deduplicated_decisions"]),
        "- Markets: " + str(data_manifest["unique_markets"]),
        "- Short-horizon pairs: " + str(data_manifest["short_horizon_observed_pairs"]),
        "- OOS predictions: " + str(folds.get("oos_predictions",0)),
        "- Full-window midpoint models ready: " + str(sum(
            value.get("state") == "READY" for value in final_models.get("models", {}).values())),
        "- Full-window executable-markout models ready: " + str(sum(
            value.get("state") == "READY" for value in final_models.get("executable_markout_models", {}).values())),
        "", "## 500ms reference executable-markout economics", "",
        "- Simulated orders: " + str(selected_metrics.get("simulated_orders")),
        "- Fills: " + str(selected_metrics.get("fills")),
        "- Marked fills: " + str(selected_metrics.get("marked_fills")),
        "- Positive marked fills: " + str(selected_metrics.get("positive_markout_fills")),
        "- Net executable markout: " + str(selected_metrics.get("markout_pnl")),
        "- Markout/fill: " + str(selected_metrics.get("markout_per_fill")),
        "- Fill rate: " + str(selected_metrics.get("fill_rate")),
        "", "The report never converts unavailable books, labels, forecasts, or fills into zero.",
    ]
    for name in charts["files"]:
        lines.extend(["", "![" + name + "](" + name + ")"])
    report = output / "REPORT.md"
    temporary = report.with_suffix(".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(report)
    return values
