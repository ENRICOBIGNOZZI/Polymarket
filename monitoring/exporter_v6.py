from __future__ import annotations

import time
from http.server import ThreadingHTTPServer
from typing import Sequence

from exporter import ExporterHandler, Metrics, _float, _read_json, parse_args
from exporter_v4 import V4Collector

EXPORTER_V6_VERSION = "1.0.0"


class V6Collector(V4Collector):
    def collect(self) -> str:
        base = super().collect()
        metrics = Metrics()
        status = _read_json(self.run_root / "runtime_status.json") or {}
        metrics.sample("polymarket_v6_exporter_info", 1, help_text="Static V6 model-specific exporter metadata.", labels={"version": EXPORTER_V6_VERSION})
        for name, row in (status.get("strategies") or {}).items():
            if not isinstance(row, dict):
                continue
            labels = {"model": str(name)}
            metrics.sample("polymarket_model_info", 1, help_text="V6 independent economic model metadata.", labels=labels)
            metrics.sample("polymarket_model_equity_usd", _float(row.get("equity")), help_text="V6 paper equity by model.", labels=labels)
            metrics.sample("polymarket_model_pnl_usd", _float(row.get("pnl")), help_text="V6 paper PnL by model.", labels=labels)
            metrics.sample("polymarket_model_open_positions", _float(row.get("live_units")), help_text="V6 live units by model.", labels=labels)
            metrics.sample("polymarket_model_kill_switch", 1 if row.get("killed") else 0, help_text="V6 model-local kill state.", labels=labels)
        relations = status.get("relations") if isinstance(status.get("relations"), dict) else {}
        metrics.sample("polymarket_v6_relation_bundles", _float(relations.get("bundles")), help_text="Current executable graph/structural V6 bundles.")
        metrics.sample("polymarket_v6_relation_best_edge_ratio", _float(relations.get("best_edge")), help_text="Best current graph/structural maker edge.")
        bridge = status.get("external_bridge") if isinstance(status.get("external_bridge"), dict) else {}
        metrics.sample("polymarket_v6_external_signals", _float(bridge.get("materialized_signals")), help_text="OOS-approved external probabilities materialized for V6.")
        metrics.sample("polymarket_v6_scrape_timestamp_seconds", time.time(), help_text="V6 exporter scrape timestamp.")
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
