from __future__ import annotations

import time
from collections import Counter
from pathlib import Path
from typing import Any, Sequence
from http.server import ThreadingHTTPServer

from exporter import ExporterHandler, Metrics, _float, _last_csv_row, _mtime, _read_csv, _read_json, parse_args
from exporter_v4 import V4Collector

EXPORTER_V6_VERSION = "1.2.0"
MODEL_FRESH_SECONDS = 120.0


def _age(path: Path, now: float) -> float:
    stamp = _mtime(path)
    return max(0.0, now - stamp) if stamp is not None else 1e12


def _fill_counts(path: Path, action_key: str = "action") -> Counter[str]:
    return Counter(str(row.get(action_key, "UNKNOWN") or "UNKNOWN").upper() for row in _read_csv(path))


class V6Collector(V4Collector):
    def _details(self, now: float) -> dict[str, dict[str, Any]]:
        root = self.run_root
        out: dict[str, dict[str, Any]] = {}

        maker_path = root / "maker" / "maker_equity.csv"
        maker = _last_csv_row(maker_path) or {}
        maker_positions = _read_csv(root / "maker" / "maker_positions.csv")
        maker_fills = _fill_counts(root / "maker" / "maker_fills.csv")
        out["micro_maker"] = {
            "source": maker_path,
            "cash": _float(maker.get("cash")),
            "peak": _float(maker.get("peak_equity")),
            "drawdown": _float(maker.get("drawdown")),
            "gross": _float(maker.get("reserved_cash")) + sum(max(0.0, _float(r.get("shares"))) * max(0.0, _float(r.get("entry_price"))) for r in maker_positions),
            "realized": 0.0,
            "fills": maker_fills,
        }

        micro_path = root / "micro_taker" / "status.json"
        micro = _read_json(micro_path) or {}
        out["micro_taker"] = {
            "source": micro_path,
            "cash": _float(micro.get("cash")),
            "peak": _float(micro.get("peak_equity")),
            "drawdown": _float(micro.get("drawdown")),
            "gross": _float(micro.get("gross_exposure")),
            "realized": _float(micro.get("realized_pnl")),
            "fills": _fill_counts(root / "micro_taker" / "fills.csv"),
        }

        rv_path = root / "multileg_equity.csv"
        rv = _last_csv_row(rv_path) or {}
        rv_events = [r for r in _read_csv(root / "multileg_events.csv") if "FILL" in str(r.get("event", "")).upper()]
        rv_fill_counts = Counter(str(r.get("event", "FILL") or "FILL").upper() for r in rv_events)
        out["relative_value"] = {
            "source": rv_path,
            "cash": _float(rv.get("cash")),
            "peak": _float(rv.get("peak_equity")),
            "drawdown": _float(rv.get("drawdown")),
            "gross": _float(rv.get("gross_entry_cash")),
            "realized": sum(_float(r.get("net_pnl")) for r in _read_csv(root / "bundle_ledger.csv")),
            "fills": rv_fill_counts,
        }

        hard_path = root / "hard_arb" / "status.json"
        hard = _read_json(hard_path) or {}
        out["graph_hard"] = {
            "source": hard_path,
            "cash": _float(hard.get("cash")),
            "peak": _float(hard.get("peak"), _float(hard.get("equity"))),
            "drawdown": _float(hard.get("drawdown")),
            "gross": _float(hard.get("gross_exposure")),
            "realized": _float(hard.get("realized_pnl")),
            "fills": _fill_counts(root / "hard_arb" / "fills.csv"),
        }

        external_path = root / "external" / "status.json"
        external = _read_json(external_path) or {}
        out["external"] = {
            "source": external_path,
            "cash": _float(external.get("cash")),
            "peak": _float(external.get("peak_equity"), _float(external.get("equity"))),
            "drawdown": _float(external.get("drawdown")),
            "gross": _float(external.get("gross_exposure")),
            "realized": _float(external.get("realized_pnl")),
            "fills": _fill_counts(root / "external" / "fills.csv"),
        }

        for row in out.values():
            source = row.get("source")
            age = _age(source, now) if isinstance(source, Path) else 1e12
            row["age"] = age
            row["alive"] = 1.0 if age <= MODEL_FRESH_SECONDS else 0.0
        return out

    def collect(self) -> str:
        base = super().collect()
        metrics = Metrics()
        now = time.time()
        status = _read_json(self.run_root / "runtime_status.json") or {}
        allocator = _read_json(self.run_root / "allocator_status.json") or {}
        details = self._details(now)

        metrics.sample("polymarket_v6_exporter_info", 1, help_text="Static V6 model-specific exporter metadata.", labels={"version": EXPORTER_V6_VERSION})
        metrics.sample("polymarket_allocator_state_present", 1 if allocator else 0, help_text="Whether V6 allocator compatibility state is present.")
        metrics.sample("polymarket_allocator_models_expected", _float(allocator.get("models_expected"), 5), help_text="Number of V6 model sleeves expected by the live manifest.")
        metrics.sample("polymarket_allocator_models_alive", sum(_float(v.get("alive")) for v in details.values()), help_text="Number of V6 model sleeves with fresh native status output.")
        metrics.sample("polymarket_allocator_global_gross_fraction", _float(allocator.get("global_gross_fraction")), help_text="V6 aggregate gross exposure divided by starting paper capital.")

        for name, row in (status.get("strategies") or {}).items():
            if not isinstance(row, dict):
                continue
            labels = {"model": str(name), "expert": str(name)}
            detail = details.get(str(name), {})
            equity = _float(row.get("equity"))
            pnl = _float(row.get("pnl"))
            starting = equity - pnl
            fields = {
                "polymarket_model_info": (1.0, "V6 independent economic model metadata."),
                "polymarket_model_starting_capital_usd": (starting, "Initial paper capital allocated to the V6 model."),
                "polymarket_model_cash_usd": (_float(detail.get("cash"), equity), "Current paper cash by V6 model."),
                "polymarket_model_equity_usd": (equity, "Current marked paper equity by V6 model."),
                "polymarket_model_pnl_usd": (pnl, "Current marked paper PnL by V6 model."),
                "polymarket_model_realized_pnl_usd": (_float(detail.get("realized")), "Current realized paper PnL by V6 model."),
                "polymarket_model_peak_equity_usd": (_float(detail.get("peak"), max(starting, equity)), "Historical peak paper equity by V6 model."),
                "polymarket_model_drawdown_ratio": (_float(detail.get("drawdown")), "Current drawdown by V6 model."),
                "polymarket_model_gross_exposure_usd": (_float(detail.get("gross")), "Current gross/reserved paper exposure by V6 model."),
                "polymarket_model_open_positions": (_float(row.get("live_units")), "Current live positions/orders/bundles by V6 model."),
                "polymarket_model_kill_switch": (1.0 if row.get("killed") else 0.0, "V6 model-local kill state."),
                "polymarket_model_alive": (_float(detail.get("alive")), "Whether the model has a fresh native V6 status snapshot."),
                "polymarket_model_status_age_seconds": (_float(detail.get("age"), 1e12), "Age in seconds of the model native V6 status snapshot."),
            }
            for metric, (value, help_text) in fields.items():
                metrics.sample(metric, value, help_text=help_text, labels=labels)

            fill_counts = detail.get("fills") if isinstance(detail.get("fills"), Counter) else Counter()
            metrics.sample("polymarket_model_fills_total", sum(fill_counts.values()), help_text="Cumulative paper fill events by V6 model.", metric_type="counter", labels={**labels, "action": "all"})
            for action in ("BUY", "SELL", "SETTLE", "BUY_COMPLETE_YES_SET"):
                if fill_counts.get(action, 0):
                    metrics.sample("polymarket_model_fills_total", fill_counts[action], help_text="Cumulative paper fill events by V6 model and action.", metric_type="counter", labels={**labels, "action": action.lower()})

            if "signals" in row:
                signals = _float(row.get("signals"))
                metrics.sample("polymarket_model_signal_window_rows", signals, help_text="Current V6 forward signals/candidates by model.", labels=labels)
                metrics.sample("polymarket_model_signals_total", signals, help_text="Compatibility alias for current V6 forward signals/candidates.", labels=labels)
                if name == "micro_taker":
                    metrics.sample("polymarket_model_cost_positive_signals_total", signals, help_text="Micro-taker signals already positive after configured execution costs.", labels=labels)
                    metrics.sample("polymarket_model_net_positive_signals_total", signals, help_text="Micro-taker signals already above the configured final net-edge gate.", labels=labels)
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

        proxy = _read_json(self.run_root / "market_proxy_status.json") or {}
        proxy_age = _age(self.run_root / "market_proxy_status.json", now) if proxy else 1e12
        metrics.sample("polymarket_v6_market_proxy_info", 1 if proxy else 0, help_text="V6 resilient public-market discovery source.", labels={"source": str(proxy.get("source", "missing"))})
        metrics.sample("polymarket_v6_market_proxy_markets", _float(proxy.get("markets")), help_text="Markets currently available through the V6 discovery proxy.")
        metrics.sample("polymarket_v6_market_proxy_gamma_ok", 1 if proxy.get("upstream_gamma_ok") else 0, help_text="Whether the latest V6 market discovery used healthy Gamma upstream data.")
        metrics.sample("polymarket_v6_market_proxy_failures_total", _float(proxy.get("failures")), help_text="Cumulative V6 public-market proxy upstream failures.", metric_type="counter")
        metrics.sample("polymarket_v6_market_proxy_status_age_seconds", proxy_age, help_text="Age of the latest V6 market-proxy status snapshot.")
        metrics.sample("polymarket_v6_market_proxy_cache_age_seconds", _float(proxy.get("cache_age_seconds")), help_text="Age of metadata cache used by the latest V6 discovery response.")
        metrics.sample("polymarket_v6_scrape_timestamp_seconds", now, help_text="V6 exporter scrape timestamp.")
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
