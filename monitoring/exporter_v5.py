from __future__ import annotations

import time
from typing import Sequence
from http.server import ThreadingHTTPServer

from exporter import ExporterHandler, Metrics, _float, _mtime, _read_csv, _read_json, parse_args
from exporter_v4 import V4Collector

EXPORTER_V5_VERSION = "1.2.0"
START_EVENTS = {"start", "restart"}
MODEL_OUTPUT_MAX_AGE_SECONDS = 120.0
MODEL_STARTUP_GRACE_SECONDS = 600.0


class V5Collector(V4Collector):
    def collect(self) -> str:
        base = super().collect()
        metrics = Metrics()
        now = time.time()
        allocator = _read_json(self.run_root / "allocator_status.json") or {}
        rows = _read_csv(self.run_root / "strategy_status.csv")
        events = _read_csv(self.run_root / "allocator_events.csv")
        latest_start: dict[str, float] = {}
        for event in events:
            name = str(event.get("strategy", ""))
            if event.get("event") not in START_EVENTS or not name:
                continue
            timestamp = _float(event.get("timestamp"), -1.0)
            if timestamp < 0.0:
                continue
            latest_start[name] = max(timestamp, latest_start.get(name, float("-inf")))

        metrics.sample(
            "polymarket_v5_exporter_info",
            1,
            help_text="Static information about the V5 independent-strategy exporter.",
            labels={"version": EXPORTER_V5_VERSION, "run_root": str(self.run_root)},
        )
        metrics.sample(
            "polymarket_allocator_state_present",
            1 if allocator else 0,
            help_text="Whether the V5 aggregate allocator status is present.",
        )
        metrics.sample(
            "polymarket_allocator_models_alive",
            _float(allocator.get("models_alive")),
            help_text="Number of independent V5 model processes currently alive.",
        )
        metrics.sample(
            "polymarket_allocator_models_expected",
            _float(allocator.get("models_expected")),
            help_text="Number of enabled independent V5 model processes.",
        )
        metrics.sample(
            "polymarket_allocator_reserve_fraction",
            _float(allocator.get("reserve_fraction")),
            help_text="Fraction of V5 parent paper capital held as unallocated reserve.",
        )
        metrics.sample(
            "polymarket_allocator_global_gross_fraction",
            _float(allocator.get("global_gross_fraction")),
            help_text="Aggregate V5 gross exposure divided by parent starting capital.",
        )
        metrics.sample(
            "polymarket_allocator_global_max_gross_fraction",
            _float(allocator.get("global_max_gross_fraction")),
            help_text="Configured V5 aggregate gross-exposure cap.",
        )
        metrics.sample(
            "polymarket_allocator_global_max_drawdown_ratio",
            _float(allocator.get("global_max_drawdown"), 0.15),
            help_text="Configured V5 aggregate drawdown kill threshold.",
        )

        compaction_path = self.run_root / "compaction_status.json"
        compaction = _read_json(compaction_path) or {}
        completed = _float(compaction.get("completed_timestamp"), _mtime(compaction_path) or now)
        paused = compaction.get("paused_pids") if isinstance(compaction.get("paused_pids"), list) else []
        metrics.sample(
            "polymarket_log_compaction_state_present",
            1 if compaction else 0,
            help_text="Whether bounded V5 strategy-log compaction has published status.",
        )
        metrics.sample(
            "polymarket_log_compaction_success",
            1 if compaction.get("success") else 0,
            help_text="Whether the latest bounded V5 strategy-log compaction completed successfully.",
        )
        metrics.sample(
            "polymarket_log_compaction_staleness_seconds",
            max(0.0, now - completed) if compaction else 0.0,
            help_text="Age of the latest bounded V5 strategy-log compaction status.",
        )
        metrics.sample(
            "polymarket_log_compaction_bytes_reclaimed",
            _float(compaction.get("bytes_reclaimed")),
            help_text="Bytes reclaimed by the latest bounded V5 strategy-log compaction.",
        )
        metrics.sample(
            "polymarket_log_compaction_duration_seconds",
            _float(compaction.get("duration_seconds")),
            help_text="Duration of the latest bounded V5 strategy-log compaction.",
        )
        metrics.sample(
            "polymarket_log_compaction_paused_processes",
            len(paused),
            help_text="Number of model processes paused during the latest atomic compaction.",
        )

        for row in rows:
            model_name = row.get("name", "unknown")
            labels = {"model": model_name, "expert": row.get("expert", "unknown")}
            status_age = max(0.0, _float(row.get("status_age_seconds")))
            start_timestamp = latest_start.get(model_name)
            start_age = now - start_timestamp if start_timestamp is not None else float("inf")
            startup_grace_active = (
                _float(row.get("alive")) > 0.0
                and status_age > MODEL_OUTPUT_MAX_AGE_SECONDS
                and start_timestamp is not None
                and -5.0 <= start_age <= MODEL_STARTUP_GRACE_SECONDS
            )
            alert_staleness = 0.0 if startup_grace_active else status_age
            fields = {
                "polymarket_model_info": (1.0, "Independent V5 paper model metadata."),
                "polymarket_model_capital_fraction": (_float(row.get("capital_fraction")), "Fraction of V5 parent paper capital allocated to the model."),
                "polymarket_model_starting_capital_usd": (_float(row.get("starting_capital")), "Initial paper capital allocated to the model."),
                "polymarket_model_cash_usd": (_float(row.get("cash")), "Current paper cash by model."),
                "polymarket_model_equity_usd": (_float(row.get("equity")), "Current marked paper equity by model."),
                "polymarket_model_pnl_usd": (_float(row.get("pnl")), "Current marked paper PnL by model."),
                "polymarket_model_realized_pnl_usd": (_float(row.get("realized_pnl")), "Current realized paper PnL by model."),
                "polymarket_model_peak_equity_usd": (_float(row.get("peak_equity")), "Historical peak paper equity by model."),
                "polymarket_model_drawdown_ratio": (_float(row.get("drawdown")), "Current drawdown by model."),
                "polymarket_model_gross_exposure_usd": (_float(row.get("gross_exposure")), "Current marked gross exposure by model."),
                "polymarket_model_open_positions": (_float(row.get("open_positions")), "Current open positions by model."),
                "polymarket_model_kill_switch": (_float(row.get("killed")), "Model-local paper kill-switch state."),
                "polymarket_model_alive": (_float(row.get("alive")), "Whether the independent model process is alive."),
                "polymarket_model_status_age_seconds": (status_age, "Raw age of the model status snapshot."),
                "polymarket_model_alert_staleness_seconds": (
                    alert_staleness,
                    "Model status age used for alerting; zero only during bounded pre-first-output startup grace.",
                ),
                "polymarket_model_startup_grace_active": (
                    1.0 if startup_grace_active else 0.0,
                    "Whether the live model is inside the bounded 600-second pre-first-output startup grace.",
                ),
                "polymarket_model_start_age_seconds": (
                    max(0.0, start_age) if start_timestamp is not None else 1e12,
                    "Age of the latest allocator start or restart event for the model.",
                ),
                "polymarket_model_restarts_total": (_float(row.get("restarts")), "Allocator-observed process restarts by model."),
            }
            for name, (value, help_text) in fields.items():
                metric_type = "counter" if name.endswith("_total") else "gauge"
                metrics.sample(name, value, help_text=help_text, metric_type=metric_type, labels=labels)

            for action, column in (("all", "fills"), ("buy", "buy_fills"), ("sell", "sell_fills"), ("settle", "settle_fills")):
                metrics.sample(
                    "polymarket_model_fills_total",
                    _float(row.get(column)),
                    help_text="Cumulative paper fills by independent model and action.",
                    metric_type="counter",
                    labels={**labels, "action": action},
                )

            signal_rows = _read_csv(self.run_root / "strategies" / row.get("name", "") / "signals.csv")
            all_rows = len(signal_rows)
            cost_positive = sum(1 for signal in signal_rows if _float(signal.get("cost_adjusted_edge")) > 0.0)
            net_positive = sum(1 for signal in signal_rows if _float(signal.get("net_edge")) > 0.0)
            metrics.sample(
                "polymarket_model_signal_window_rows",
                all_rows,
                help_text="Rows in the bounded recent signal window for an independent V5 model.",
                labels=labels,
            )
            metrics.sample(
                "polymarket_model_cost_positive_signal_window_rows",
                cost_positive,
                help_text="Rows positive after fee and slippage in the bounded recent signal window.",
                labels=labels,
            )
            metrics.sample(
                "polymarket_model_net_positive_signal_window_rows",
                net_positive,
                help_text="Rows with positive final net edge in the bounded recent signal window.",
                labels=labels,
            )
            # Deprecated compatibility names retained for the existing dashboard;
            # they are gauges over a bounded recent window, not lifetime counters.
            metrics.sample(
                "polymarket_model_signals_total",
                all_rows,
                help_text="Deprecated alias for bounded recent signal-window rows.",
                labels=labels,
            )
            metrics.sample(
                "polymarket_model_cost_positive_signals_total",
                cost_positive,
                help_text="Deprecated alias for bounded recent cost-positive signal-window rows.",
                labels=labels,
            )
            metrics.sample(
                "polymarket_model_net_positive_signals_total",
                net_positive,
                help_text="Deprecated alias for bounded recent net-positive signal-window rows.",
                labels=labels,
            )
            metrics.sample(
                "polymarket_model_best_net_edge_ratio",
                max((_float(signal.get("net_edge")) for signal in signal_rows), default=0.0),
                help_text="Best net executable edge in the bounded recent signal window.",
                labels=labels,
            )

        metrics.sample(
            "polymarket_allocator_scrape_timestamp_seconds",
            now,
            help_text="Unix timestamp of the current V5 allocator scrape.",
        )
        return base + metrics.render()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    collector = V5Collector(args.runs_root, args.config, args.top_opportunities)
    ExporterHandler.collector = collector
    server = ThreadingHTTPServer((args.host, args.port), ExporterHandler)
    print(f"polymarket V5 exporter listening on http://{args.host}:{args.port}/metrics", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


COLLECTOR_CLASS = V5Collector


if __name__ == "__main__":
    raise SystemExit(main())
