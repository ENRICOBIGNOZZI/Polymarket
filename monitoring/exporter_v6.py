from __future__ import annotations

import time
from http.server import ThreadingHTTPServer
from typing import Sequence

from exporter import ExporterHandler, Metrics, _float, _read_json, parse_args
from exporter_v4 import V4Collector

EXPORTER_V6_VERSION = "1.1.0"


class V6Collector(V4Collector):
    def collect(self) -> str:
        base = super().collect()
        metrics = Metrics()
        status = _read_json(self.run_root / "runtime_status.json") or {}
        allocator = _read_json(self.run_root / "allocator_status.json") or {}
        metrics.sample("polymarket_v6_exporter_info", 1, help_text="Static V6 model-specific exporter metadata.", labels={"version": EXPORTER_V6_VERSION})
        for name, row in (status.get("strategies") or {}).items():
            if not isinstance(row, dict):
                continue
            labels = {"model": str(name), "expert": str(name)}
            metrics.sample("polymarket_model_info", 1, help_text="V6 independent economic model metadata.", labels=labels)
            metrics.sample("polymarket_model_equity_usd", _float(row.get("equity")), help_text="V6 paper equity by model.", labels=labels)
            metrics.sample("polymarket_model_pnl_usd", _float(row.get("pnl")), help_text="V6 paper PnL by model.", labels=labels)
            metrics.sample("polymarket_model_open_positions", _float(row.get("live_units")), help_text="V6 live units by model.", labels=labels)
            metrics.sample("polymarket_model_kill_switch", 1 if row.get("killed") else 0, help_text="V6 model-local kill state.", labels=labels)
            if "signals" in row:
                metrics.sample("polymarket_model_signal_window_rows", _float(row.get("signals")), help_text="Current V6 forward signals by model.", labels=labels)
            if "best_edge" in row:
                metrics.sample("polymarket_model_best_net_edge_ratio", _float(row.get("best_edge")), help_text="Best current V6 executable edge by model.", labels=labels)
        relations = status.get("relations") if isinstance(status.get("relations"), dict) else {}
        local_factor = status.get("local_factor") if isinstance(status.get("local_factor"), dict) else {}
        bridge = status.get("external_bridge") if isinstance(status.get("external_bridge"), dict) else {}
        metrics.sample("polymarket_v6_relation_bundles", _float(relations.get("bundles")), help_text="Current executable graph/structural V6 bundles.")
        metrics.sample("polymarket_v6_relation_best_edge_ratio", _float(relations.get("best_edge")), help_text="Best current graph/structural maker edge.")
        metrics.sample("polymarket_v6_local_factor_bundles", _float(local_factor.get("bundles")), help_text="Current local-factor bundles.")
        metrics.sample("polymarket_v6_local_factor_clusters", _float(local_factor.get("clusters")), help_text="Current local factor clusters evaluated.")
        metrics.sample("polymarket_v6_external_signals", _float(bridge.get("materialized_signals")), help_text="OOS-approved external probabilities materialized for V6.")
        metrics.sample("polymarket_v6_scrape_timestamp_seconds", time.time(), help_text="V6 exporter scrape timestamp.")

        # Transitional metrics consumed by the already-installed V5-aware server
        # updater. They describe V6 health only; no V5 allocator/expert execution is
        # restored by this compatibility surface.
        metrics.sample("polymarket_allocator_state_present", 1 if allocator else 0, help_text="Legacy health compatibility view for V6.")
        metrics.sample("polymarket_allocator_models_expected", _float(allocator.get("models_expected"), 5), help_text="Legacy health compatibility count.")
        metrics.sample("polymarket_allocator_models_alive", _float(allocator.get("models_alive"), 5), help_text="Legacy health compatibility alive count.")
        metrics.sample("polymarket_allocator_global_gross_fraction", _float(allocator.get("global_gross_fraction")), help_text="V6 gross fraction exposed through legacy health metric.")
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
