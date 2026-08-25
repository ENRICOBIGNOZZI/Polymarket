from __future__ import annotations

import time
from http.server import ThreadingHTTPServer
from typing import Sequence

from exporter import ExporterHandler, Metrics, _float, _read_json, parse_args
from exporter_v4 import V4Collector

EXPORTER_V6_VERSION = "2.0.1"


def _label(value: object, limit: int = 120) -> str:
    return str(value or "")[:limit]


class V6Collector(V4Collector):
    def collect(self) -> str:
        base = super().collect()
        metrics = Metrics()
        status = _read_json(self.run_root / "runtime_status.json") or {}
        allocator = _read_json(self.run_root / "allocator_status.json") or {}

        metrics.sample("polymarket_v6_exporter_info", 1, help_text="Static V6 model-specific exporter metadata.", labels={"version": EXPORTER_V6_VERSION})
        metrics.sample("polymarket_v6_open_orders", _float(status.get("open_order_count")), help_text="Current V6 paper open orders.")
        metrics.sample("polymarket_v6_fills_total", _float(status.get("fill_count_total")), help_text="Durable V6 paper fill count.")
        metrics.sample("polymarket_v6_realized_pnl_usd", _float(status.get("realized_pnl")), help_text="Aggregate V6 realized paper PnL.")

        for name, row in (status.get("strategies") or {}).items():
            if not isinstance(row, dict):
                continue
            labels = {"model": str(name), "expert": str(name)}
            metrics.sample("polymarket_model_info", 1, help_text="V6 independent economic model metadata.", labels=labels)
            metrics.sample("polymarket_model_equity_usd", _float(row.get("equity")), help_text="V6 paper equity by model.", labels=labels)
            metrics.sample("polymarket_model_pnl_usd", _float(row.get("pnl")), help_text="V6 marked PnL by model.", labels=labels)
            metrics.sample("polymarket_model_realized_pnl_usd", _float(row.get("realized_pnl")), help_text="V6 realized paper PnL by model.", labels=labels)
            metrics.sample("polymarket_model_gross_exposure_usd", _float(row.get("gross_exposure")), help_text="V6 gross exposure by model.", labels=labels)
            metrics.sample("polymarket_model_drawdown_ratio", _float(row.get("drawdown")), help_text="V6 drawdown by model.", labels=labels)
            metrics.sample("polymarket_model_open_positions", _float(row.get("live_units")), help_text="V6 live orders/positions/bundles by model.", labels=labels)
            metrics.sample("polymarket_model_orders_total", _float(row.get("orders_total")), help_text="Durable V6 orders posted by model.", labels=labels)
            metrics.sample("polymarket_model_fills_total", _float(row.get("fills_total")), help_text="Durable V6 fills by model.", labels=labels)
            metrics.sample("polymarket_model_status_age_seconds", _float(row.get("status_age_seconds")), help_text="Age of underlying model status file.", labels=labels)
            metrics.sample("polymarket_model_alive", 1 if _float(row.get("status_age_seconds"), 1e9) <= 180 else 0, help_text="V6 model freshness health.", labels=labels)
            metrics.sample("polymarket_model_kill_switch", 1 if row.get("killed") else 0, help_text="V6 model-local kill state.", labels=labels)
            if "signals" in row:
                metrics.sample("polymarket_model_signals_total", _float(row.get("signals")), help_text="Current V6 executable signal count.", labels=labels)
            if "best_edge" in row:
                metrics.sample("polymarket_model_best_net_edge_ratio", _float(row.get("best_edge")), help_text="Best current V6 executable edge.", labels=labels)
            if "labeled_samples" in row:
                metrics.sample("polymarket_model_labeled_samples", _float(row.get("labeled_samples")), help_text="Current causal labeled samples.", labels=labels)

        for strategy, row in (status.get("sub_strategies") or {}).items():
            if not isinstance(row, dict):
                continue
            labels = {"strategy": str(strategy)}
            metrics.sample("polymarket_strategy_realized_pnl_usd", _float(row.get("realized_pnl")), help_text="Realized multileg PnL by V6 strategy.", labels=labels)
            metrics.sample("polymarket_strategy_finalized_bundles_total", _float(row.get("finalized_bundles")), help_text="Finalized multileg bundles by V6 strategy.", labels=labels)
            metrics.sample("polymarket_strategy_live_bundles", _float(row.get("live_bundles")), help_text="Live multileg bundles by V6 strategy.", labels=labels)

        for order in status.get("open_orders") or []:
            if not isinstance(order, dict):
                continue
            labels = {
                "model": _label(order.get("model"), 32), "strategy": _label(order.get("strategy"), 48),
                "bundle_id": _label(order.get("bundle_id"), 80), "market_id": _label(order.get("market_id"), 80),
                "side": _label(order.get("side"), 8), "state": _label(order.get("state"), 24),
                "limit_price": f"{_float(order.get('limit_price')):.6g}",
            }
            metrics.sample("polymarket_open_order_remaining_shares", _float(order.get("remaining_shares")), help_text="Current individual V6 paper order remaining shares.", labels=labels)
            metrics.sample("polymarket_open_order_queue_ahead_shares", _float(order.get("queue_ahead")), help_text="Current conservative queue ahead for an individual V6 paper order.", labels=labels)

        for fill in status.get("recent_fills") or []:
            if not isinstance(fill, dict):
                continue
            labels = {
                "model": _label(fill.get("model"), 32), "strategy": _label(fill.get("strategy"), 48),
                "market_id": _label(fill.get("market_id"), 80), "side": _label(fill.get("side"), 8),
                "action": _label(fill.get("action"), 32), "timestamp": str(int(_float(fill.get("timestamp")))),
                "price": f"{_float(fill.get('price')):.6g}", "shares": f"{_float(fill.get('shares')):.6g}",
            }
            metrics.sample("polymarket_recent_fill_pnl_usd", _float(fill.get("pnl")), help_text="Recent individual V6 paper fill PnL when realized on that row.", labels=labels)

        relations = status.get("relations") if isinstance(status.get("relations"), dict) else {}
        guard = status.get("relation_guard") if isinstance(status.get("relation_guard"), dict) else {}
        queue_filter = status.get("queue_filter") if isinstance(status.get("queue_filter"), dict) else {}
        local_factor = status.get("local_factor") if isinstance(status.get("local_factor"), dict) else {}
        structural = status.get("typed_structural") if isinstance(status.get("typed_structural"), dict) else {}
        bridge = status.get("external_bridge") if isinstance(status.get("external_bridge"), dict) else {}

        metrics.sample("polymarket_v6_relation_bundles", _float(relations.get("bundles")), help_text="Raw graph relation bundles.")
        metrics.sample("polymarket_v6_relation_guard_accepted_bundles", _float(guard.get("accepted_bundles")), help_text="Graph relation bundles after economic guard.")
        metrics.sample("polymarket_v6_queue_filter_accepted_bundles", _float(queue_filter.get("accepted_bundles")), help_text="Fill-aware maker bundles admitted to broker.")
        metrics.sample("polymarket_v6_queue_filter_improved_bundles", _float(queue_filter.get("improved_bundles")), help_text="Maker bundles whose quote was improved using edge budget.")
        metrics.sample("polymarket_v6_queue_filter_best_joint_fill_probability", _float(queue_filter.get("best_joint_fill_probability")), help_text="Best current joint fill probability proxy.")
        metrics.sample("polymarket_v6_queue_filter_best_expected_fill_edge_ratio", _float(queue_filter.get("best_expected_fill_edge")), help_text="Best edge times joint-fill proxy.")
        metrics.sample("polymarket_v6_queue_filter_max_queue_ahead_shares", _float(queue_filter.get("max_queue_ahead")), help_text="Largest queue ahead observed by V6 queue filter.")
        metrics.sample("polymarket_v6_local_factor_clusters", _float(local_factor.get("clusters")), help_text="Current V6 local-factor cluster count.")
        metrics.sample("polymarket_v6_local_factor_bundles", _float(local_factor.get("bundles")), help_text="Current fill-aware local-factor bundles.")
        metrics.sample("polymarket_v6_local_factor_candidates", _float(local_factor.get("candidate_count")), help_text="Local factor residual candidates before FDR.")
        metrics.sample("polymarket_v6_local_factor_fdr_survivors", _float(local_factor.get("fdr_survivors")), help_text="Local factor block-bootstrap FDR survivors.")
        metrics.sample("polymarket_v6_typed_structural_bundles", _float(structural.get("bundles")), help_text="Fail-closed typed structural bundles.")
        metrics.sample("polymarket_v6_external_signals", _float(bridge.get("materialized_signals")), help_text="OOS-approved external probabilities materialized for V6.")
        metrics.sample("polymarket_v6_scrape_timestamp_seconds", time.time(), help_text="V6 exporter scrape timestamp.")

        metrics.sample("polymarket_allocator_state_present", 1 if allocator else 0, help_text="V6 health compatibility view.")
        metrics.sample("polymarket_allocator_models_expected", _float(allocator.get("models_expected"), 5), help_text="Expected V6 model sleeves.")
        metrics.sample("polymarket_allocator_models_alive", _float(allocator.get("models_alive"), 0), help_text="Fresh V6 model sleeves.")
        metrics.sample("polymarket_allocator_global_gross_fraction", _float(allocator.get("global_gross_fraction")), help_text="V6 global gross fraction.")
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
