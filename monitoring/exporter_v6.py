from __future__ import annotations

import time
from http.server import ThreadingHTTPServer
from typing import Sequence

from exporter import ExporterHandler, Metrics, _float, _mtime, _read_json, parse_args
from exporter_v4 import V4Collector

EXPORTER_V6_VERSION = "1.3.0"
V6_MODEL_FRESH_SECONDS = 120.0


class V6Collector(V4Collector):
    def collect(self) -> str:
        base = super().collect()
        metrics = Metrics()
        now = time.time()
        status_path = self.run_root / "runtime_status.json"
        status = _read_json(status_path) or {}
        allocator = _read_json(self.run_root / "allocator_status.json") or {}
        status_age = max(0.0, now - (_mtime(status_path) or now)) if status else 1e12
        model_alive = 1.0 if status and status_age <= V6_MODEL_FRESH_SECONDS else 0.0
        alert_staleness = 0.0 if status_age <= V6_MODEL_FRESH_SECONDS else status_age

        metrics.sample(
            "polymarket_v6_exporter_info",
            1,
            help_text="Static V6 model-specific exporter metadata.",
            labels={"version": EXPORTER_V6_VERSION},
        )
        for name, row in (status.get("strategies") or {}).items():
            if not isinstance(row, dict):
                continue
            labels = {"model": str(name), "expert": str(name)}
            fields = {
                "polymarket_model_info": (1.0, "V6 independent economic model metadata."),
                "polymarket_model_capital_fraction": (_float(row.get("capital_fraction")), "Fraction of V6 parent paper capital allocated to the model."),
                "polymarket_model_starting_capital_usd": (_float(row.get("starting_capital")), "Initial V6 paper capital allocated to the model."),
                "polymarket_model_cash_usd": (_float(row.get("cash")), "Current V6 paper cash by model."),
                "polymarket_model_equity_usd": (_float(row.get("equity")), "V6 paper equity by model."),
                "polymarket_model_pnl_usd": (_float(row.get("pnl")), "V6 paper PnL by model."),
                "polymarket_model_realized_pnl_usd": (_float(row.get("realized_pnl")), "V6 realized paper PnL by model where the sleeve exposes a durable realized ledger."),
                "polymarket_model_gross_exposure_usd": (_float(row.get("gross_exposure")), "V6 committed/executed gross exposure by model."),
                "polymarket_model_drawdown_ratio": (_float(row.get("drawdown")), "V6 model-local paper drawdown."),
                "polymarket_model_open_positions": (_float(row.get("live_units")), "V6 live orders, bundles, or positions by model."),
                "polymarket_model_kill_switch": (1.0 if row.get("killed") else 0.0, "V6 model-local kill state."),
                "polymarket_model_alive": (model_alive, "Whether the monolithic V6 runtime status is fresh enough to consider each model alive."),
                "polymarket_model_status_age_seconds": (status_age, "Age of the canonical V6 runtime-status snapshot."),
                "polymarket_model_alert_staleness_seconds": (alert_staleness, "V6 model staleness used by alerts after a bounded two-report grace window."),
                "polymarket_model_startup_grace_active": (1.0 if status and status_age <= V6_MODEL_FRESH_SECONDS else 0.0, "Compatibility health flag: V6 runtime is inside the bounded freshness window."),
            }
            for metric_name, (value, help_text) in fields.items():
                metrics.sample(metric_name, value, help_text=help_text, labels=labels)

            # V6 strategies publish executable/net-positive candidate counts rather
            # than V5's three-stage signal file. Preserve the dashboard aliases with
            # the same executable count; never invent additional pre-cost signals.
            signals = _float(row.get("signals"))
            metrics.sample("polymarket_model_signal_window_rows", signals, help_text="Current executable V6 signals by model.", labels=labels)
            metrics.sample("polymarket_model_signals_total", signals, help_text="Compatibility alias for current executable V6 signal rows.", labels=labels)
            metrics.sample("polymarket_model_cost_positive_signals_total", signals, help_text="Compatibility alias: V6 reported signals are already cost-positive/executable.", labels=labels)
            metrics.sample("polymarket_model_net_positive_signals_total", signals, help_text="Compatibility alias: V6 reported signals are already net-positive/executable.", labels=labels)
            metrics.sample("polymarket_model_best_net_edge_ratio", _float(row.get("best_edge")), help_text="Best current V6 executable edge by model.", labels=labels)

            fill_columns = (
                ("all", "fills"),
                ("buy", "buy_fills"),
                ("sell", "sell_fills"),
                ("settle", "settle_fills"),
            )
            for action, column in fill_columns:
                metrics.sample(
                    "polymarket_model_fills_total",
                    _float(row.get(column)),
                    help_text="Cumulative V6 simulated fill events by model and action, sourced from durable execution ledgers.",
                    metric_type="counter",
                    labels={**labels, "action": action},
                )

            # Dedicated aliases retained for any V6 consumers introduced before
            # the stable action-labelled Grafana contract was restored.
            metrics.sample("polymarket_model_buy_fills_total", _float(row.get("buy_fills")), help_text="Cumulative V6 entry/buy fill events by model.", metric_type="counter", labels=labels)
            metrics.sample("polymarket_model_sell_fills_total", _float(row.get("sell_fills")), help_text="Cumulative V6 exit/sell fill events by model.", metric_type="counter", labels=labels)
            metrics.sample("polymarket_model_settle_fills_total", _float(row.get("settle_fills")), help_text="Cumulative V6 settlement fill events by model.", metric_type="counter", labels=labels)

        relations = status.get("relations") if isinstance(status.get("relations"), dict) else {}
        local_factor = status.get("local_factor") if isinstance(status.get("local_factor"), dict) else {}
        bridge = status.get("external_bridge") if isinstance(status.get("external_bridge"), dict) else {}
        metrics.sample("polymarket_v6_relation_bundles", _float(relations.get("bundles")), help_text="Current executable graph/structural V6 bundles.")
        metrics.sample("polymarket_v6_relation_best_edge_ratio", _float(relations.get("best_edge")), help_text="Best current graph/structural maker edge.")
        metrics.sample("polymarket_v6_local_factor_bundles", _float(local_factor.get("bundles")), help_text="Current local-factor bundles.")
        metrics.sample("polymarket_v6_local_factor_clusters", _float(local_factor.get("clusters")), help_text="Current local factor clusters evaluated.")
        metrics.sample("polymarket_v6_external_signals", _float(bridge.get("materialized_signals")), help_text="OOS-approved external probabilities materialized for V6.")
        metrics.sample("polymarket_v6_scrape_timestamp_seconds", now, help_text="V6 exporter scrape timestamp.")

        # Transitional metrics consumed by stable monitoring/server health checks.
        # They describe V6 only; no V5 allocator/expert execution is restored.
        metrics.sample("polymarket_allocator_state_present", 1 if allocator else 0, help_text="Legacy health compatibility view for V6.")
        metrics.sample("polymarket_allocator_models_expected", _float(allocator.get("models_expected"), 5), help_text="V6 model count exposed through stable health metric.")
        metrics.sample("polymarket_allocator_models_alive", _float(allocator.get("models_alive"), 5) if model_alive else 0.0, help_text="V6 alive model count gated by canonical runtime freshness.")
        metrics.sample("polymarket_allocator_reserve_fraction", _float(allocator.get("reserve_fraction")), help_text="V6 parent paper reserve fraction.")
        metrics.sample("polymarket_allocator_global_gross_fraction", _float(allocator.get("global_gross_fraction")), help_text="V6 gross fraction exposed through stable health metric.")
        metrics.sample("polymarket_allocator_global_max_gross_fraction", _float(allocator.get("global_max_gross_fraction"), 0.45), help_text="Configured V6 aggregate gross-exposure cap.")
        metrics.sample("polymarket_allocator_global_max_drawdown_ratio", _float(allocator.get("global_max_drawdown"), 0.15), help_text="Configured V6 aggregate drawdown kill threshold.")
        return base + metrics.render()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    collector = V6Collector(args.runs_root, args.config, args.top_opportunities)
    ExporterHandler.collector = collector
    server = ThreadingHTTPServer((args.host, args.port), ExporterHandler)
    print(f"polymarket V6 exporter listening on http://{args.host}:{args.port}/metrics", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


COLLECTOR_CLASS = V6Collector


if __name__ == "__main__":
    raise SystemExit(main())
