from __future__ import annotations

import argparse
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Sequence

from exporter_common import ExporterHandler, Metrics, _float, _mtime, _read_csv, _read_json

EXPORTER_V7_VERSION = "2.0.0"


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


class V7Collector:
    """Canonical monitoring adapter for the unified V7 PAPER runtime only."""

    def __init__(self, run_root: Path, config: Path, top_opportunities: int = 20) -> None:
        self.v7_root = Path(run_root)
        self.execution_root = self.v7_root / "execution"
        if not self.execution_root.is_dir() and (self.v7_root / "runtime_status.json").exists():
            self.execution_root = self.v7_root
        self.shadow_root = self.v7_root / "shadow"
        self.config_path = Path(config)
        self.top_opportunities = max(1, int(top_opportunities))

    def health(self, max_staleness_seconds: float = 180.0) -> tuple[bool, str]:
        status_path = self.execution_root / "runtime_status.json"
        status = _read_json(status_path) or {}
        if not status:
            return False, "v7_runtime_status_missing"
        if int(_float(status.get("version"))) != 7:
            return False, "v7_runtime_version_mismatch"
        if not _bool(status.get("paper_only", False)):
            return False, "v7_runtime_not_paper_only"
        if _bool(status.get("authenticated_execution", False)):
            return False, "v7_authenticated_execution_enabled"
        timestamp = _float(status.get("timestamp"), _mtime(status_path) or 0.0)
        if timestamp <= 0:
            return False, "v7_runtime_timestamp_missing"
        age = max(0.0, time.time() - timestamp)
        if age > max_staleness_seconds:
            return False, f"v7_runtime_stale:{age:.1f}s"
        supervisor = _read_json(self.v7_root / "v7_supervisor.json") or {}
        if supervisor and not _bool(supervisor.get("execution_alive")):
            return False, "v7_execution_child_not_alive"
        return True, "ok"

    def collect(self) -> str:
        now = time.time()
        metrics = Metrics()
        status_path = self.execution_root / "runtime_status.json"
        status = _read_json(status_path) or {}
        allocator = _read_json(self.execution_root / "allocator_status.json") or {}
        strategies = status.get("strategies") if isinstance(status.get("strategies"), dict) else {}
        strategy_rows = {
            str(row.get("name") or row.get("expert") or ""): row
            for row in _read_csv(self.execution_root / "strategy_status.csv")
            if row.get("name") or row.get("expert")
        }

        metrics.sample(
            "polymarket_runtime_info",
            1,
            help_text="Selected canonical Polymarket runtime and monitoring adapter.",
            labels={"version": "v7", "run_root": self.v7_root.name, "adapter": "v7"},
        )
        metrics.sample("polymarket_v7_runtime_info", 1, help_text="Unified V7 PAPER monitoring adapter active.")

        runtime_fields = {
            "polymarket_runtime_equity_usd": (status.get("equity"), "Canonical V7 marked equity."),
            "polymarket_runtime_pnl_usd": (status.get("pnl"), "Canonical V7 marked PnL."),
            "polymarket_runtime_drawdown_ratio": (status.get("drawdown"), "Canonical V7 drawdown ratio."),
            "polymarket_runtime_kill_switch": (1 if _bool(status.get("killed")) else 0, "Canonical V7 kill-switch state."),
            "polymarket_runtime_live_units": (status.get("live_units"), "Canonical V7 live positions/orders/bundles."),
            "polymarket_runtime_reserved_cash_usd": (status.get("reserved_cash"), "Canonical V7 reserved cash."),
            "polymarket_runtime_gross_exposure_usd": (status.get("gross_exposure"), "Canonical V7 gross exposure."),
            "polymarket_runtime_realized_pnl_usd_total": (status.get("realized_pnl"), "Canonical V7 cumulative realized paper PnL."),
            "polymarket_runtime_execution_imbalance_ratio": (status.get("execution_imbalance"), "Canonical V7 cross-leg execution imbalance."),
            "polymarket_runtime_execution_staleness_seconds": (status.get("execution_staleness"), "Canonical V7 execution-data staleness."),
        }
        for name, (value, help_text) in runtime_fields.items():
            metrics.sample(name, _float(value), help_text=help_text)

        metrics.sample("polymarket_allocator_state_present", 1 if allocator else 0, help_text="Whether the canonical V7 allocator state is present.")
        metrics.sample("polymarket_allocator_models_expected", _float(allocator.get("models_expected"), len(strategies)), help_text="Expected V7 strategy sleeves.")
        metrics.sample("polymarket_allocator_models_alive", _float(allocator.get("models_alive"), len(strategies)), help_text="Alive V7 strategy sleeves.")

        names = sorted(set(strategies) | set(strategy_rows))
        for model in names:
            raw = strategies.get(model) if isinstance(strategies.get(model), dict) else {}
            row = strategy_rows.get(model, {})
            labels = {"model": model}
            metrics.sample("polymarket_model_info", 1, help_text="Canonical V7 strategy sleeve present.", labels=labels)
            metrics.sample("polymarket_model_equity_usd", _float(raw.get("equity"), _float(row.get("equity"))), help_text="V7 strategy marked equity.", labels=labels)
            metrics.sample("polymarket_model_pnl_usd", _float(raw.get("pnl"), _float(row.get("pnl"))), help_text="V7 strategy marked PnL.", labels=labels)
            metrics.sample("polymarket_model_realized_pnl_usd_total", _float(raw.get("realized_pnl"), _float(row.get("realized_pnl"))), help_text="V7 strategy cumulative realized PnL.", labels=labels)
            metrics.sample("polymarket_model_drawdown_ratio", _float(raw.get("drawdown"), _float(row.get("drawdown"))), help_text="V7 strategy drawdown ratio.", labels=labels)
            metrics.sample("polymarket_model_gross_exposure_usd", _float(raw.get("gross_exposure"), _float(row.get("gross_exposure"))), help_text="V7 strategy gross exposure.", labels=labels)
            metrics.sample("polymarket_model_open_positions", _float(raw.get("live_units"), _float(row.get("open_positions"))), help_text="V7 strategy live units.", labels=labels)
            metrics.sample("polymarket_model_alive", _float(row.get("alive"), 1.0), help_text="V7 strategy liveness state.", labels=labels)
            metrics.sample("polymarket_model_alert_staleness_seconds", _float(row.get("status_age_seconds")), help_text="V7 strategy status staleness.", labels=labels)
            metrics.sample("polymarket_model_fills_total", _float(raw.get("fills"), _float(row.get("fills"))), help_text="V7 strategy durable fills.", labels={**labels, "action": "all"})
            for action, key in (("buy", "buy_fills"), ("sell", "sell_fills"), ("settle", "settle_fills")):
                metrics.sample("polymarket_model_fills_total", _float(raw.get(key), _float(row.get(key))), help_text="V7 strategy durable fills by action.", labels={**labels, "action": action})

        supervisor = _read_json(self.v7_root / "v7_supervisor.json") or {}
        metrics.sample("polymarket_v7_execution_alive", 1 if _bool(supervisor.get("execution_alive")) else 0, help_text="V7 execution child liveness.")
        metrics.sample("polymarket_v7_shadow_alive", 1 if _bool(supervisor.get("shadow_alive")) else 0, help_text="V7 shadow scheduler liveness.")

        pca = _read_json(self.shadow_root / "pca_stat_arb.json") or {}
        metrics.sample("polymarket_v7_pca_bh_survivors", _float(pca.get("bh_survivors"), _float(pca.get("selected_hypotheses"))), help_text="V7 PCA statistically selected residual hypotheses.")
        metrics.sample("polymarket_v7_pca_shadow_candidates", _float(pca.get("shadow_candidates"), _float(pca.get("candidate_count"))), help_text="V7 PCA fixed-horizon shadow candidates.")
        metrics.sample("polymarket_v7_pca_promotion_ready", 1 if _bool(pca.get("promotion_ready")) else 0, help_text="V7 PCA promotion gate state.")

        for fidelity, filename in (("30m", "local_factor_30m.json"), ("60m", "local_factor_60m.json")):
            local = _read_json(self.shadow_root / filename) or {}
            labels = {"fidelity": fidelity}
            metrics.sample("polymarket_v7_local_factor_by_selected_pairs", _float(local.get("by_selected_pairs")), help_text="V7 Local Factor BY-FDR selected pairs.", labels=labels)
            metrics.sample("polymarket_v7_local_factor_signals", _float(local.get("post_multiplicity_pair_signals")), help_text="V7 Local Factor post-multiplicity pair signals.", labels=labels)
            metrics.sample("polymarket_v7_local_factor_promotion_ready", 1 if _bool(local.get("promotion_ready")) else 0, help_text="V7 Local Factor promotion gate state.", labels=labels)

        ranking = _read_json(self.shadow_root / "cross_sectional_rank.json") or {}
        for row in ranking.get("forward", []) if isinstance(ranking.get("forward"), list) else []:
            horizon = str(int(_float(row.get("horizon_minutes"))))
            labels = {"horizon_minutes": horizon}
            metrics.sample("polymarket_v7_rank_completed_sections", _float(row.get("completed_sections")), help_text="Prospective V7 ranking completed sections.", labels=labels)
            metrics.sample("polymarket_v7_rank_mean_ic", _float(row.get("mean_rank_ic")), help_text="Prospective V7 ranking mean rank IC.", labels=labels)
            metrics.sample("polymarket_v7_rank_tail_spread", _float(row.get("mean_top_bottom_logit_spread")), help_text="Prospective V7 top-minus-bottom relative-logit spread.", labels=labels)
            metrics.sample("polymarket_v7_rank_statistical_gate", 1 if _bool(row.get("forward_statistical_gate")) else 0, help_text="Per-horizon V7 ranking statistical gate.", labels=labels)

        hf = _read_json(self.shadow_root / "hf_frequency_probe.json") or {}
        for row in hf.get("cadences", []) if isinstance(hf.get("cadences"), list) else []:
            cadence = str(int(_float(row.get("cadence_seconds"))))
            labels = {"cadence_seconds": cadence}
            metrics.sample("polymarket_v7_hf_nonempty_bucket_fraction", _float(row.get("nonempty_bucket_fraction")), help_text="V7 same-tape public-flow density by decision cadence.", labels=labels)
            metrics.sample("polymarket_v7_hf_maker_clearable_fraction", _float(row.get("maker_clearable_fraction")), help_text="V7 maker queues clearable inside one cadence bucket.", labels=labels)
            metrics.sample("polymarket_v7_hf_max_clearance_ratio", _float(row.get("max_best_queue_clearance_ratio")), help_text="V7 maximum queue clearance ratio by cadence.", labels=labels)

        shadow_status = _read_json(self.shadow_root / "scheduler_status.json") or {}
        shadow_ts = _float(shadow_status.get("timestamp"), now)
        metrics.sample("polymarket_v7_shadow_staleness_seconds", max(0.0, now - shadow_ts), help_text="Age of V7 multi-frequency shadow scheduler state.")
        status_ts = _float(status.get("timestamp"), _mtime(status_path) or now)
        metrics.sample("polymarket_v7_runtime_staleness_seconds", max(0.0, now - status_ts), help_text="Age of canonical V7 runtime state.")
        return metrics.render()


class V7ExporterHandler(ExporterHandler):
    collector: V7Collector

    def do_GET(self) -> None:
        if self.path == "/healthz":
            healthy, detail = self.collector.health()
            body = (detail + "\n").encode("utf-8")
            self.send_response(200 if healthy else 503)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


def parse_args(argv: Sequence[str] | None = None):
    parser = argparse.ArgumentParser(description="Unified V7 PAPER Prometheus exporter")
    parser.add_argument("--runs-base", type=Path, default=Path("runs"))
    parser.add_argument("--run-name", default="paper_v7_live")
    parser.add_argument("--config-dir", type=Path, default=Path("config"))
    parser.add_argument("--config", default="paper_v7.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9108)
    parser.add_argument("--top-opportunities", type=int, default=20)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    run_root = args.runs_base / args.run_name
    config = Path(args.config)
    if not config.is_absolute():
        config = args.config_dir / config
    collector = V7Collector(run_root, config, args.top_opportunities)
    V7ExporterHandler.collector = collector
    server = ThreadingHTTPServer((args.host, args.port), V7ExporterHandler)
    print(f"polymarket V7 exporter listening on http://{args.host}:{args.port}/metrics", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


COLLECTOR_CLASS = V7Collector


if __name__ == "__main__":
    raise SystemExit(main())
