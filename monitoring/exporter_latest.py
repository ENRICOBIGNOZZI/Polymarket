from __future__ import annotations

import argparse
import importlib
import json
import re
import time
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Sequence

from exporter import Collector, ExporterHandler, Metrics, _float, _last_csv_row, _mtime, _read_json

_VERSION_RE = re.compile(r"paper_v(\d+(?:[._-]\d+)*)", re.IGNORECASE)


def _version_tuple(text: str) -> tuple[int, ...]:
    m = _VERSION_RE.search(text)
    if not m:
        return ()
    return tuple(int(x) for x in re.findall(r"\d+", m.group(1)))


def _version_label(v: tuple[int, ...]) -> str:
    return "v" + ".".join(str(x) for x in v) if v else "unknown"


def detect_run_root(runs_base: Path, run_name: str) -> tuple[Path, tuple[int, ...]]:
    if run_name and run_name.lower() != "auto":
        root = runs_base / run_name
        return root, _version_tuple(run_name)
    candidates = [p for p in runs_base.iterdir() if p.is_dir() and _version_tuple(p.name)] if runs_base.exists() else []
    if not candidates:
        return runs_base / "paper_v4_live", (4,)
    candidates.sort(key=lambda p: (_version_tuple(p.name), p.stat().st_mtime_ns, p.name))
    root = candidates[-1]
    return root, _version_tuple(root.name)


def detect_config(config_dir: Path, version: tuple[int, ...], explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    if version:
        exact = config_dir / f"paper_v{version[0]}.json"
        if exact.exists():
            return exact
    configs = [p for p in config_dir.glob("paper_v*.json") if _version_tuple(p.name)]
    if configs:
        configs.sort(key=lambda p: (_version_tuple(p.name), p.name))
        return configs[-1]
    return config_dir / "paper_v4.json"


def collector_class(version: tuple[int, ...]):
    # Detailed adapters are optional. The stable dashboard never depends on one:
    # future engines can publish runtime_status.json and be monitored immediately.
    major = version[0] if version else 0
    for v in range(major, 3, -1):
        try:
            module = importlib.import_module(f"exporter_v{v}")
        except ImportError:
            continue
        cls = getattr(module, "COLLECTOR_CLASS", None) or getattr(module, f"V{v}Collector", None)
        if cls is not None:
            return cls, f"v{v}"
    return Collector, "base"


@dataclass
class CanonicalStatus:
    equity: float = 0.0
    pnl: float = 0.0
    drawdown: float = 0.0
    killed: float = 0.0
    live_units: float = 0.0
    reserved_cash: float = 0.0
    gross_exposure: float = 0.0
    realized_pnl: float = 0.0
    execution_imbalance: float = 0.0
    execution_staleness: float = 0.0
    oos_trades: float = 0.0
    oos_net_pnl: float = 0.0
    oos_stressed_net_pnl: float = 0.0
    oos_drawdown: float = 0.0
    oos_pvalue: float = 1.0
    oos_eligible: float = 0.0
    production_threshold: float = 0.0
    oos_staleness: float = 0.0


def _canonical_from_contract(root: Path, now: float) -> CanonicalStatus | None:
    raw = _read_json(root / "runtime_status.json")
    if not raw:
        return None
    oos = raw.get("oos") if isinstance(raw.get("oos"), dict) else {}
    return CanonicalStatus(
        equity=_float(raw.get("equity")), pnl=_float(raw.get("pnl")), drawdown=_float(raw.get("drawdown")),
        killed=1.0 if bool(raw.get("killed")) else 0.0, live_units=_float(raw.get("live_units")),
        reserved_cash=_float(raw.get("reserved_cash")), gross_exposure=_float(raw.get("gross_exposure")),
        realized_pnl=_float(raw.get("realized_pnl")), execution_imbalance=_float(raw.get("execution_imbalance")),
        execution_staleness=_float(raw.get("execution_staleness")), oos_trades=_float(oos.get("trades")),
        oos_net_pnl=_float(oos.get("net_pnl")), oos_stressed_net_pnl=_float(oos.get("stressed_net_pnl")),
        oos_drawdown=_float(oos.get("max_drawdown")), oos_pvalue=_float(oos.get("bootstrap_pvalue"), 1.0),
        oos_eligible=1.0 if bool(oos.get("eligible_for_tiny_pilot")) else 0.0,
        production_threshold=_float(oos.get("production_threshold")),
        oos_staleness=max(0.0, now - (_mtime(root / "runtime_status.json") or now)),
    )


def _canonical_fallback(root: Path, config: Path, now: float) -> CanonicalStatus:
    cfg = _read_json(config) or {}
    starting = _float(cfg.get("starting_capital"), 10000.0)
    eq_path = root / "multileg_equity.csv"
    row = _last_csv_row(eq_path) or {}
    equity = _float(row.get("equity"), starting)
    recorder_mtime = _mtime(root / "trade_tape.csv")
    broker_ts = _float(row.get("timestamp"), _mtime(eq_path) or now)
    component_ages = [max(0.0, now - broker_ts)]
    if recorder_mtime is not None:
        component_ages.append(max(0.0, now - recorder_mtime))

    # Fill imbalance is derived from durable leg state, so the canonical dashboard
    # remains correct even when the detailed version adapter changes.
    imbalance = 0.0
    try:
        import csv
        by_bundle: dict[str, list[float]] = {}
        with (root / "multileg_legs.csv").open(newline="", encoding="utf-8") as f:
            for leg in csv.DictReader(f):
                target = max(0.0, _float(leg.get("target_shares")))
                filled = max(0.0, _float(leg.get("filled_shares")))
                frac = min(1.0, max(0.0, filled / target)) if target > 1e-12 else 0.0
                by_bundle.setdefault(leg.get("bundle_id", ""), []).append(frac)
        for fractions in by_bundle.values():
            if fractions:
                imbalance = max(imbalance, max(fractions) - min(fractions))
    except OSError:
        pass

    realized = 0.0
    try:
        import csv
        with (root / "bundle_ledger.csv").open(newline="", encoding="utf-8") as f:
            realized = sum(_float(r.get("net_pnl")) for r in csv.DictReader(f))
    except OSError:
        pass

    report_path = root / "walk_forward.json"
    report = _read_json(report_path) or {}
    oos = report.get("oos") if isinstance(report.get("oos"), dict) else {}
    stress = report.get("oos_cost_stress") if isinstance(report.get("oos_cost_stress"), dict) else {}
    return CanonicalStatus(
        equity=equity,
        pnl=equity - starting,
        drawdown=_float(row.get("drawdown")),
        killed=_float(row.get("killed")),
        live_units=_float(row.get("live_bundles")),
        reserved_cash=_float(row.get("reserved_cash")),
        gross_exposure=_float(row.get("gross_entry_cash")),
        realized_pnl=realized,
        execution_imbalance=imbalance,
        execution_staleness=max(component_ages) if component_ages else 0.0,
        oos_trades=_float(oos.get("trades")),
        oos_net_pnl=_float(oos.get("net_pnl")),
        oos_stressed_net_pnl=_float(stress.get("net_pnl")),
        oos_drawdown=_float(oos.get("max_drawdown")),
        oos_pvalue=_float(report.get("bootstrap_one_sided_pvalue"), 1.0),
        oos_eligible=1.0 if report.get("eligible_for_tiny_pilot") else 0.0,
        production_threshold=_float(report.get("production_threshold")),
        oos_staleness=max(0.0, now - (_mtime(report_path) or now)) if report else 0.0,
    )


def _collect_all_market_metrics(metrics: Metrics, root: Path, now: float) -> None:
    universe_path = root / "all_market" / "universe_status.json"
    universe = _read_json(universe_path) or {}
    metrics.sample(
        "polymarket_all_market_universe_present",
        1.0 if universe else 0.0,
        help_text="Whether the complete active-market inventory is present.",
    )
    metrics.sample(
        "polymarket_all_market_universe_markets",
        _float(universe.get("markets")),
        help_text="Number of active tradable markets in the all-market inventory.",
    )
    metrics.sample(
        "polymarket_all_market_tier1_markets",
        _float(universe.get("tier1")),
        help_text="Markets admitted to the event-driven Tier-1 compute universe.",
    )
    metrics.sample(
        "polymarket_all_market_tier2_markets",
        _float(universe.get("tier2")),
        help_text="Markets admitted to the historical/statistical Tier-2 compute universe.",
    )
    metrics.sample(
        "polymarket_all_market_universe_staleness_seconds",
        max(0.0, now - _float(universe.get("generated_ts"), now)) if universe else 0.0,
        help_text="Age of the all-market universe inventory.",
    )

    book_path = root / "global_opportunity_status.json"
    book = _read_json(book_path) or {}
    metrics.sample(
        "polymarket_global_opportunity_book_present",
        1.0 if book else 0.0,
        help_text="Whether the global ranked opportunity book is present.",
    )
    metrics.sample(
        "polymarket_global_research_candidates",
        _float(book.get("research_candidates")),
        help_text="Number of ranked positive-raw-edge research candidates retained globally.",
    )
    metrics.sample(
        "polymarket_global_eligible_candidates",
        _float(book.get("eligible_candidates")),
        help_text="Number of globally ranked candidates positive after their source-specific executable cost model.",
    )
    metrics.sample(
        "polymarket_global_hard_arbitrage_candidates",
        _float(book.get("hard_arbitrage_candidates")),
        help_text="Number of currently eligible hard-arbitrage candidates in the global book.",
    )
    metrics.sample(
        "polymarket_global_best_net_edge",
        _float(book.get("best_net_edge")),
        help_text="Best net edge in the globally eligible candidate book.",
    )
    metrics.sample(
        "polymarket_global_best_expected_profit_usd",
        _float(book.get("best_expected_profit")),
        help_text="Best expected paper profit in the globally eligible candidate book.",
    )
    metrics.sample(
        "polymarket_global_opportunity_staleness_seconds",
        max(0.0, now - _float(book.get("generated_ts"), now)) if book else 0.0,
        help_text="Age of the global opportunity ranking.",
    )

    fast_path = root / "fast" / "fast_arb_status.json"
    fast = _read_json(fast_path) or {}
    metrics.sample(
        "polymarket_fast_feed_present",
        1.0 if fast else 0.0,
        help_text="Whether the event-driven all-market fast feed has a runtime status.",
    )
    metrics.sample(
        "polymarket_fast_markets",
        _float(fast.get("markets")),
        help_text="Markets subscribed by the event-driven fast shadow engine.",
    )
    metrics.sample(
        "polymarket_fast_current_executable",
        _float(fast.get("current_executable")),
        help_text="Current executable opportunities in the fast shadow engine.",
    )
    metrics.sample(
        "polymarket_fast_current_hard_executable",
        _float(fast.get("current_hard_executable")),
        help_text="Current hard-arbitrage opportunities in the fast shadow engine.",
    )
    metrics.sample(
        "polymarket_fast_best_net_edge",
        _float(fast.get("best_net_edge_per_share")),
        help_text="Best current fast-engine net edge per share.",
    )
    metrics.sample(
        "polymarket_fast_feed_stale_ms",
        _float(fast.get("feed_stale_ms"), -1.0),
        help_text="Milliseconds since the last public market WebSocket message.",
    )
    metrics.sample(
        "polymarket_fast_ws_workers",
        _float(fast.get("ws_workers")),
        help_text="Configured public market WebSocket shards.",
    )
    metrics.sample(
        "polymarket_fast_ws_connected_workers",
        _float(fast.get("ws_connected_workers")),
        help_text="Connected public market WebSocket shards.",
    )
    metrics.sample(
        "polymarket_fast_decision_latency_p95_us",
        _float(fast.get("decision_latency_p95_us")),
        help_text="95th percentile event-to-decision compute latency for the fast engine.",
    )

    account = _read_json(root / "account_readonly_status.json") or {}
    metrics.sample(
        "polymarket_account_readonly_configured",
        1.0 if bool(account.get("configured")) else 0.0,
        help_text="Whether optional read-only Polymarket account reconciliation is configured.",
    )
    metrics.sample(
        "polymarket_account_readonly_healthy",
        1.0 if account.get("status") == "healthy" else 0.0,
        help_text="Whether optional GET-only Polymarket account reconciliation is healthy.",
    )
    metrics.sample(
        "polymarket_account_open_orders",
        _float(account.get("open_orders")),
        help_text="Open orders reported by optional read-only account reconciliation.",
    )


class LatestCollector:
    def __init__(self, runs_base: Path, config_dir: Path, run_name: str, explicit_config: str | None, top_opportunities: int) -> None:
        self.runs_base = runs_base
        self.config_dir = config_dir
        self.run_name = run_name
        self.explicit_config = explicit_config
        self.top_opportunities = top_opportunities
        self._delegate_key: tuple[str, str] | None = None
        self._delegate = None
        self._adapter = "base"

    def _resolve(self):
        root, version = detect_run_root(self.runs_base, self.run_name)
        config = detect_config(self.config_dir, version, self.explicit_config)
        key = (str(root), str(config))
        if self._delegate is None or key != self._delegate_key:
            cls, adapter = collector_class(version)
            self._delegate = cls(root, config, self.top_opportunities)
            self._delegate_key = key
            self._adapter = adapter
        return root, version, config

    def collect(self) -> str:
        root, version, config = self._resolve()
        now = time.time()
        try:
            detailed = self._delegate.collect()
        except Exception:
            detailed = ""
        status = _canonical_from_contract(root, now) or _canonical_fallback(root, config, now)
        metrics = Metrics()
        labels = {"version": _version_label(version), "run_root": root.name, "adapter": self._adapter}
        metrics.sample("polymarket_runtime_info", 1, help_text="Selected latest Polymarket runtime and monitoring adapter.", labels=labels)
        fields = {
            "polymarket_runtime_equity_usd": (status.equity, "Canonical latest-runtime marked equity."),
            "polymarket_runtime_pnl_usd": (status.pnl, "Canonical latest-runtime marked PnL."),
            "polymarket_runtime_drawdown_ratio": (status.drawdown, "Canonical latest-runtime drawdown ratio."),
            "polymarket_runtime_kill_switch": (status.killed, "Canonical latest-runtime kill-switch state."),
            "polymarket_runtime_live_units": (status.live_units, "Canonical latest-runtime live bundles/positions."),
            "polymarket_runtime_reserved_cash_usd": (status.reserved_cash, "Canonical latest-runtime reserved cash."),
            "polymarket_runtime_gross_exposure_usd": (status.gross_exposure, "Canonical latest-runtime gross execution exposure."),
            "polymarket_runtime_realized_pnl_usd_total": (status.realized_pnl, "Canonical latest-runtime cumulative realized paper PnL."),
            "polymarket_runtime_execution_imbalance_ratio": (status.execution_imbalance, "Canonical latest-runtime cross-leg execution imbalance."),
            "polymarket_runtime_execution_staleness_seconds": (status.execution_staleness, "Worst canonical execution-data staleness."),
            "polymarket_runtime_oos_trades": (status.oos_trades, "Canonical latest-runtime selected OOS trade count."),
            "polymarket_runtime_oos_net_pnl_usd": (status.oos_net_pnl, "Canonical latest-runtime OOS net PnL."),
            "polymarket_runtime_oos_stressed_net_pnl_usd": (status.oos_stressed_net_pnl, "Canonical latest-runtime stressed OOS net PnL."),
            "polymarket_runtime_oos_drawdown_ratio": (status.oos_drawdown, "Canonical latest-runtime OOS maximum drawdown."),
            "polymarket_runtime_oos_bootstrap_pvalue": (status.oos_pvalue, "Canonical latest-runtime OOS bootstrap p-value."),
            "polymarket_runtime_oos_eligible": (status.oos_eligible, "Canonical latest-runtime real-money escalation gate."),
            "polymarket_runtime_production_threshold": (status.production_threshold, "Canonical latest-runtime forward edge threshold."),
            "polymarket_runtime_oos_staleness_seconds": (status.oos_staleness, "Age of the canonical latest-runtime OOS report."),
        }
        for name, (value, help_text) in fields.items():
            metrics.sample(name, value, help_text=help_text)
        _collect_all_market_metrics(metrics, root, now)
        return detailed + metrics.render()


def parse_latest_args(argv: Sequence[str] | None = None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-base", type=Path, default=Path("runs"))
    ap.add_argument("--run-name", default="auto", help="Versioned run directory name or 'auto'.")
    ap.add_argument("--config-dir", type=Path, default=Path("config"))
    ap.add_argument("--config", default=None, help="Optional explicit config path; otherwise selected by runtime version.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9108)
    ap.add_argument("--top-opportunities", type=int, default=20)
    return ap.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_latest_args(argv)
    collector = LatestCollector(args.runs_base, args.config_dir, args.run_name, args.config, args.top_opportunities)
    ExporterHandler.collector = collector
    server = ThreadingHTTPServer((args.host, args.port), ExporterHandler)
    print(f"polymarket latest-version exporter listening on http://{args.host}:{args.port}/metrics", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
