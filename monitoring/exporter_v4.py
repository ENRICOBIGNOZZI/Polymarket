from __future__ import annotations
import time
from collections import Counter, defaultdict
from typing import Mapping, Sequence
from http.server import ThreadingHTTPServer

from exporter import Collector, ExporterHandler, Metrics, _float, _last_csv_row, _mtime, _read_csv, _read_json, parse_args

EXPORTER_V4_VERSION = "1.1.0"


def _safe_ratio(a: float, b: float) -> float:
    return a / b if abs(b) > 1e-12 else 0.0


class V4Collector(Collector):
    def collect(self) -> str:
        base = super().collect()
        now = time.time()
        metrics = Metrics()
        starting_capital, max_drawdown = self._config()
        metrics.sample("polymarket_v4_exporter_info", 1, help_text="Static information about the V4 execution/OOS exporter.", labels={"version": EXPORTER_V4_VERSION, "run_root": str(self.run_root)})
        self._trade_recorder_v4(metrics, now)
        self._multileg_v4(metrics, now, starting_capital, max_drawdown)
        self._terminal_v4(metrics, now, starting_capital)
        self._oos_v4(metrics, now)
        return base + metrics.render()

    def _trade_recorder_v4(self, metrics: Metrics, now: float) -> None:
        tape = self.run_root / "trade_tape.csv"
        modified = _mtime(tape)
        rows = _read_csv(tape)
        metrics.sample("polymarket_trade_recorder_state_present", 1 if modified is not None else 0, help_text="Whether the public V4 trade tape is present.")
        metrics.sample("polymarket_trade_recorder_rows", len(rows), help_text="Number of normalized public trades currently in the V4 tape file.")
        if modified is not None:
            metrics.sample("polymarket_trade_recorder_last_update_timestamp_seconds", modified, help_text="Unix timestamp of the latest V4 public-trade tape update.")
            metrics.sample("polymarket_trade_recorder_staleness_seconds", max(0.0, now - modified), help_text="Age in seconds of the latest V4 public-trade tape update.")

    def _multileg_v4(self, metrics: Metrics, now: float, starting_capital: float, max_drawdown: float) -> None:
        equity_path = self.run_root / "multileg_equity.csv"
        eq = _last_csv_row(equity_path)
        modified = _mtime(equity_path)
        metrics.sample("polymarket_multileg_state_present", 1 if eq else 0, help_text="Whether a complete V4 multi-leg equity snapshot is available.")
        metrics.sample("polymarket_multileg_max_drawdown_ratio", max_drawdown, help_text="Configured V4 multi-leg maximum drawdown ratio.")
        if eq:
            equity = _float(eq.get("equity"), starting_capital)
            ts = _float(eq.get("timestamp"), modified or now)
            fields = {
                "polymarket_multileg_cash_usd": (_float(eq.get("cash")), "Current V4 multi-leg paper cash."),
                "polymarket_multileg_equity_usd": (equity, "Current V4 multi-leg marked paper equity."),
                "polymarket_multileg_reserved_cash_usd": (_float(eq.get("reserved_cash")), "Cash reserved by V4 resting multi-leg orders."),
                "polymarket_multileg_gross_entry_cash_usd": (_float(eq.get("gross_entry_cash")), "Gross cash committed to open V4 multi-leg fills."),
                "polymarket_multileg_peak_equity_usd": (_float(eq.get("peak_equity"), max(equity, starting_capital)), "Historical peak V4 multi-leg equity."),
                "polymarket_multileg_drawdown_ratio": (_float(eq.get("drawdown")), "Current V4 multi-leg drawdown ratio."),
                "polymarket_multileg_kill_switch": (_float(eq.get("killed")), "V4 multi-leg kill-switch state; one means active."),
                "polymarket_multileg_live_bundles": (_float(eq.get("live_bundles")), "Current number of live V4 multi-leg bundles."),
                "polymarket_multileg_pnl_usd": (equity - starting_capital, "V4 multi-leg marked PnL versus starting capital."),
                "polymarket_multileg_return_ratio": (_safe_ratio(equity, starting_capital) - 1.0 if starting_capital > 0 else 0.0, "V4 multi-leg marked return versus starting capital."),
                "polymarket_multileg_last_update_timestamp_seconds": (ts, "Unix timestamp of the latest V4 multi-leg equity snapshot."),
                "polymarket_multileg_staleness_seconds": (max(0.0, now - ts), "Age in seconds of the latest V4 multi-leg equity snapshot."),
            }
            for name, (value, help_text) in fields.items():
                metrics.sample(name, value, help_text=help_text)

        bundles = _read_csv(self.run_root / "multileg_bundles.csv")
        legs = _read_csv(self.run_root / "multileg_legs.csv")
        for status, count in sorted(Counter((r.get("status") or "UNKNOWN").upper() for r in bundles).items()):
            metrics.sample("polymarket_multileg_bundles", count, help_text="Current number of V4 multi-leg bundles by state.", labels={"status": status})

        legs_by_bundle: dict[str, list[Mapping[str, str]]] = defaultdict(list)
        for row in legs:
            legs_by_bundle[row.get("bundle_id", "")].append(row)
        max_imbalance = 0.0
        for bundle in bundles:
            bid = bundle.get("bundle_id", "")
            fractions, queue, filled_notional = [], 0.0, 0.0
            for leg in legs_by_bundle.get(bid, []):
                target = max(0.0, _float(leg.get("target_shares")))
                filled = max(0.0, _float(leg.get("filled_shares")))
                frac = min(1.0, max(0.0, _safe_ratio(filled, target))) if target > 0 else 0.0
                fractions.append(frac)
                queue += max(0.0, _float(leg.get("queue_ahead")))
                filled_notional += max(0.0, _float(leg.get("entry_cash")))
                labels = {"bundle_id": bid, "strategy": bundle.get("strategy", ""), "market_id": leg.get("market_id", ""), "side": leg.get("side", "")}
                metrics.sample("polymarket_multileg_leg_fill_fraction", frac, help_text="V4 paper fill fraction for each bundle leg.", labels=labels)
                metrics.sample("polymarket_multileg_leg_queue_ahead_shares", max(0.0, _float(leg.get("queue_ahead"))), help_text="Estimated queue ahead in shares for each V4 resting leg.", labels=labels)
            completion = min(fractions) if fractions else 0.0
            imbalance = max(fractions) - min(fractions) if fractions else 0.0
            max_imbalance = max(max_imbalance, imbalance)
            labels = {"bundle_id": bid, "strategy": bundle.get("strategy", ""), "status": bundle.get("status", "")}
            metrics.sample("polymarket_multileg_bundle_completion_ratio", completion, help_text="Minimum leg fill fraction for a V4 multi-leg bundle.", labels=labels)
            metrics.sample("polymarket_multileg_bundle_fill_imbalance_ratio", imbalance, help_text="Gap between best- and worst-filled legs in a V4 bundle.", labels=labels)
            metrics.sample("polymarket_multileg_bundle_queue_ahead_shares", queue, help_text="Total estimated queue ahead across V4 bundle legs.", labels=labels)
            metrics.sample("polymarket_multileg_bundle_filled_notional_usd", filled_notional, help_text="Entry cash already filled for a V4 bundle.", labels=labels)
        metrics.sample("polymarket_multileg_max_fill_imbalance_ratio", max_imbalance, help_text="Maximum current fill imbalance across V4 multi-leg bundles.")

        ledger = _read_csv(self.run_root / "bundle_ledger.csv")
        for status, count in sorted(Counter((r.get("status") or "UNKNOWN").upper() for r in ledger).items()):
            metrics.sample("polymarket_multileg_finalized_bundles_total", count, help_text="Cumulative finalized V4 bundle count by final state.", metric_type="counter", labels={"status": status})
        metrics.sample("polymarket_multileg_realized_net_pnl_usd_total", sum(_float(r.get("net_pnl")) for r in ledger), help_text="Cumulative realized V4 multi-leg paper net PnL.", metric_type="counter")
        metrics.sample("polymarket_multileg_realized_fees_usd_total", sum(max(0.0, _float(r.get("fees"))) for r in ledger), help_text="Cumulative realized V4 multi-leg paper fees.", metric_type="counter")
        metrics.sample("polymarket_multileg_realized_slippage_usd_total", sum(max(0.0, _float(r.get("slippage"))) for r in ledger), help_text="Cumulative realized V4 multi-leg paper slippage.", metric_type="counter")

    def _terminal_v4(self, metrics: Metrics, now: float, starting_capital: float) -> None:
        terminal = self.run_root / "terminal"
        status_path = terminal / "status.json"
        status = _read_json(status_path) or {}
        modified = _mtime(status_path)
        metrics.sample("polymarket_terminal_state_present", 1 if status else 0, help_text="Whether the continuous cost-aware taker paper engine has published a status snapshot.")
        if status:
            equity = _float(status.get("equity"), starting_capital)
            ts = _float(status.get("timestamp"), modified or now)
            fields = {
                "polymarket_terminal_cash_usd": (_float(status.get("cash"), starting_capital), "Current taker paper cash."),
                "polymarket_terminal_equity_usd": (equity, "Current taker paper marked equity."),
                "polymarket_terminal_pnl_usd": (equity - starting_capital, "Current taker paper marked PnL versus starting capital."),
                "polymarket_terminal_peak_equity_usd": (_float(status.get("peak_equity"), max(equity, starting_capital)), "Historical peak taker paper equity."),
                "polymarket_terminal_drawdown_ratio": (_float(status.get("drawdown")), "Current taker paper drawdown ratio."),
                "polymarket_terminal_gross_exposure_usd": (_float(status.get("gross_exposure")), "Current taker paper gross marked exposure."),
                "polymarket_terminal_open_positions": (_float(status.get("open_positions")), "Current number of open taker paper positions."),
                "polymarket_terminal_kill_switch": (1.0 if bool(status.get("killed")) else 0.0, "Taker paper kill-switch state; one means active."),
                "polymarket_terminal_last_update_timestamp_seconds": (ts, "Unix timestamp of the latest taker paper status snapshot."),
                "polymarket_terminal_staleness_seconds": (max(0.0, now - ts), "Age in seconds of the latest taker paper status snapshot."),
            }
            for name, (value, help_text) in fields.items():
                metrics.sample(name, value, help_text=help_text)

        fills = _read_csv(terminal / "fills.csv")
        actions = Counter((row.get("action") or "UNKNOWN").upper() for row in fills)
        metrics.sample("polymarket_terminal_fills_total", len(fills), help_text="Cumulative cost-aware taker paper fill events recorded by the terminal sleeve.", metric_type="counter")
        metrics.sample("polymarket_terminal_buy_fills_total", actions.get("BUY", 0), help_text="Cumulative taker paper BUY fill events.", metric_type="counter")
        metrics.sample("polymarket_terminal_sell_fills_total", actions.get("SELL", 0), help_text="Cumulative taker paper SELL fill events.", metric_type="counter")
        metrics.sample("polymarket_terminal_settlements_total", actions.get("SETTLE", 0), help_text="Cumulative taker paper settlement events.", metric_type="counter")
        last_fill = _last_csv_row(terminal / "fills.csv")
        if last_fill:
            fill_ts = _float(last_fill.get("timestamp"), _mtime(terminal / "fills.csv") or now)
            metrics.sample("polymarket_terminal_last_fill_timestamp_seconds", fill_ts, help_text="Unix timestamp of the latest taker paper fill.")
            metrics.sample("polymarket_terminal_last_fill_staleness_seconds", max(0.0, now - fill_ts), help_text="Age in seconds of the latest taker paper fill.")

    def _oos_v4(self, metrics: Metrics, now: float) -> None:
        path = self.run_root / "walk_forward.json"
        report = _read_json(path)
        modified = _mtime(path)
        metrics.sample("polymarket_oos_state_present", 1 if report else 0, help_text="Whether the latest V4 walk-forward report exists.")
        if not report:
            return
        oos = report.get("oos") if isinstance(report.get("oos"), dict) else {}
        stress = report.get("oos_cost_stress") if isinstance(report.get("oos_cost_stress"), dict) else {}
        metrics.sample("polymarket_oos_eligible_for_tiny_pilot", 1 if report.get("eligible_for_tiny_pilot") else 0, help_text="Whether the V4 OOS gate currently permits the tiny real-money pilot.")
        metrics.sample("polymarket_oos_input_trades", _float(report.get("input_trades")), help_text="Closed/unwound historical bundle count used by V4 walk-forward.")
        metrics.sample("polymarket_oos_selected_trades", _float(oos.get("trades")), help_text="Selected V4 out-of-sample trade count.")
        metrics.sample("polymarket_oos_net_pnl_usd", _float(oos.get("net_pnl")), help_text="V4 out-of-sample net paper PnL.")
        metrics.sample("polymarket_oos_stressed_net_pnl_usd", _float(stress.get("net_pnl")), help_text="V4 out-of-sample PnL after configured cost stress.")
        metrics.sample("polymarket_oos_max_drawdown_ratio", _float(oos.get("max_drawdown")), help_text="V4 out-of-sample maximum drawdown.")
        metrics.sample("polymarket_oos_profit_factor", _float(oos.get("profit_factor")), help_text="V4 out-of-sample profit factor.")
        metrics.sample("polymarket_oos_bootstrap_pvalue", _float(report.get("bootstrap_one_sided_pvalue"), 1.0), help_text="One-sided circular block-bootstrap p-value for positive V4 mean return.")
        metrics.sample("polymarket_oos_production_threshold", _float(report.get("production_threshold")), help_text="Forward production edge threshold chosen only from calibration data.")
        if modified is not None:
            metrics.sample("polymarket_oos_staleness_seconds", max(0.0, now - modified), help_text="Age in seconds of the latest V4 OOS report.")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    collector = V4Collector(args.runs_root, args.config, args.top_opportunities)
    ExporterHandler.collector = collector
    server = ThreadingHTTPServer((args.host, args.port), ExporterHandler)
    print(f"polymarket V4 exporter listening on http://{args.host}:{args.port}/metrics", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
