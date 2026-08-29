from __future__ import annotations

import time
from http.server import ThreadingHTTPServer
from typing import Sequence

from exporter import (
    ExporterHandler,
    Metrics,
    _float,
    _last_csv_row,
    _mtime,
    _read_csv,
    _read_json,
    parse_args,
)
from exporter_v4 import V4Collector

EXPORTER_V6_VERSION = "1.5.1"
V6_MODEL_FRESH_SECONDS = 120.0
MISSING_STATUS_AGE_SECONDS = 1e12
V6_STRATEGY_HEALTH = {
    "micro_maker": "micro",
    "micro_taker": "micro",
    "relative_value": "pca",
    "graph_hard": "graph",
    "external": "external",
}


def _label(value: object, limit: int = 120) -> str:
    return str(value or "")[:limit]


def _fill_counts(path) -> dict[str, int]:
    rows = _read_csv(path)
    buy = sell = settle = 0
    for row in rows:
        action = str(row.get("action") or "").upper()
        if action.startswith("BUY"):
            buy += 1
        elif action.startswith("SELL"):
            sell += 1
        elif action.startswith("SETTLE"):
            settle += 1
    return {"fills": len(rows), "buy_fills": buy, "sell_fills": sell, "settle_fills": settle}


def _multileg_fill_counts(path) -> dict[str, int]:
    buy = sell = settle = 0
    for row in _read_csv(path):
        event = str(row.get("event") or "").upper()
        shares = _float(row.get("shares"))
        if shares <= 0.0:
            continue
        if event == "PARTIAL_FILL":
            buy += 1
        elif event == "EXIT_TAKER":
            sell += 1
        elif event == "SETTLE":
            settle += 1
    return {
        "fills": buy + sell + settle,
        "buy_fills": buy,
        "sell_fills": sell,
        "settle_fills": settle,
    }


def _sum_csv(path, column: str) -> float:
    return sum(_float(row.get(column)) for row in _read_csv(path))


def _unique_bundle_count(path) -> int:
    return len(
        {
            str(row.get("bundle_id") or "").strip()
            for row in _read_csv(path)
            if str(row.get("bundle_id") or "").strip()
        }
    )


def _model_health(strategy_rows: dict[str, dict[str, object]], model: str) -> tuple[float, float]:
    row = strategy_rows.get(V6_STRATEGY_HEALTH.get(model, "")) or {}
    if not row:
        return 0.0, MISSING_STATUS_AGE_SECONDS
    alive = 1.0 if _float(row.get("alive")) >= 1.0 else 0.0
    age = max(0.0, _float(row.get("status_age_seconds"), MISSING_STATUS_AGE_SECONDS))
    return alive, age


def _maker_realized_pnl(path) -> float:
    rows = _read_csv(path)
    if any(str(row.get("pnl") or "").strip() for row in rows):
        return sum(_float(row.get("pnl")) for row in rows)
    inventory: dict[tuple[str, str], list[float]] = {}
    realized = 0.0
    for row in rows:
        action = str(row.get("action") or "").upper()
        key = (str(row.get("market_id") or ""), str(row.get("side") or ""))
        shares = max(0.0, _float(row.get("shares")))
        price = max(0.0, _float(row.get("price")))
        fee = max(0.0, _float(row.get("fee")))
        if shares <= 0.0:
            continue
        if action.startswith("BUY"):
            state = inventory.setdefault(key, [0.0, 0.0])
            state[0] += shares
            state[1] += shares * price + fee
        elif action.startswith("SELL") or "SETTLE" in action:
            state = inventory.setdefault(key, [0.0, 0.0])
            if state[0] <= 1e-12:
                continue
            closed = min(shares, state[0])
            average_cost = state[1] / state[0]
            fee_share = fee * (closed / shares)
            realized += closed * price - fee_share - closed * average_cost
            state[0] -= closed
            state[1] = max(0.0, state[1] - closed * average_cost)
    return realized


def _bundle_strategy_map(run_root) -> dict[str, str]:
    return {
        str(row.get("bundle_id") or ""): str(row.get("strategy") or "RELATIVE_VALUE").upper()
        for row in _read_csv(run_root / "multileg_bundles.csv")
        if row.get("bundle_id")
    }


def _open_orders(run_root, limit: int = 100) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for row in _read_csv(run_root / "maker" / "maker_orders.csv"):
        output.append(
            {
                "model": "micro_maker",
                "strategy": "MICRO_MAKER",
                "bundle_id": "",
                "market_id": row.get("market_id") or "",
                "side": row.get("side") or "",
                "state": str(row.get("state") or "RESTING").upper(),
                "limit_price": _float(row.get("limit_price")),
                "remaining_shares": _float(row.get("remaining_shares"), _float(row.get("shares"))),
                "queue_ahead": _float(row.get("queue_ahead")),
            }
        )
    bundle_strategy = _bundle_strategy_map(run_root)
    for row in _read_csv(run_root / "multileg_legs.csv"):
        state = str(row.get("order_state") or "").upper()
        if state not in {"RESTING", "CANCEL_PENDING"}:
            continue
        target = _float(row.get("target_shares"))
        filled = _float(row.get("filled_shares"))
        bundle_id = str(row.get("bundle_id") or "")
        output.append(
            {
                "model": "relative_value",
                "strategy": bundle_strategy.get(bundle_id, "RELATIVE_VALUE"),
                "bundle_id": bundle_id,
                "market_id": row.get("market_id") or "",
                "side": row.get("side") or "",
                "state": state,
                "limit_price": _float(row.get("limit_price")),
                "remaining_shares": max(0.0, target - filled),
                "queue_ahead": _float(row.get("queue_ahead")),
            }
        )
    return output[:limit]


def _recent_fills(run_root, limit: int = 60) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for model, strategy, path in (
        ("micro_maker", "MICRO_MAKER", run_root / "maker" / "maker_fills.csv"),
        ("micro_taker", "MICRO_TAKER", run_root / "micro_taker" / "fills.csv"),
        ("graph_hard", "GRAPH_HARD", run_root / "hard_arb" / "fills.csv"),
    ):
        for row in _read_csv(path)[-limit:]:
            output.append(
                {
                    "timestamp": int(_float(row.get("timestamp"))),
                    "model": model,
                    "strategy": strategy,
                    "market_id": row.get("market_id") or row.get("event_id") or "",
                    "side": row.get("side") or "",
                    "action": row.get("action") or "",
                    "shares": _float(row.get("shares")),
                    "price": _float(row.get("price")),
                    "pnl": _float(row.get("pnl")),
                }
            )
    bundle_strategy = _bundle_strategy_map(run_root)
    for row in _read_csv(run_root / "multileg_events.csv")[-200:]:
        event = str(row.get("event") or "").upper()
        if "FILL" not in event and "UNWIND" not in event and "EXIT" not in event:
            continue
        bundle_id = str(row.get("bundle_id") or "")
        output.append(
            {
                "timestamp": int(_float(row.get("timestamp"))),
                "model": "relative_value",
                "strategy": bundle_strategy.get(bundle_id, "RELATIVE_VALUE"),
                "market_id": row.get("market_id") or "",
                "side": row.get("side") or "",
                "action": event,
                "shares": _float(row.get("shares")),
                "price": _float(row.get("price")),
                "pnl": 0.0,
            }
        )
    output.sort(key=lambda row: int(row["timestamp"]), reverse=True)
    return output[:limit]


def _rv_strategy_breakdown(run_root) -> dict[str, dict[str, float]]:
    bundle_strategy = _bundle_strategy_map(run_root)
    output: dict[str, dict[str, float]] = {}
    for row in _read_csv(run_root / "bundle_ledger.csv"):
        bundle_id = str(row.get("bundle_id") or "")
        strategy = str(row.get("strategy") or bundle_strategy.get(bundle_id) or "RELATIVE_VALUE").upper()
        entry = output.setdefault(strategy, {"realized_pnl": 0.0, "finalized_bundles": 0.0, "live_bundles": 0.0})
        entry["realized_pnl"] += _float(row.get("net_pnl"))
        entry["finalized_bundles"] += 1.0
    for row in _read_csv(run_root / "multileg_bundles.csv"):
        state = str(row.get("status") or "").upper()
        if state not in {"RESTING", "COMPLETE", "ABORTING"}:
            continue
        strategy = str(row.get("strategy") or "RELATIVE_VALUE").upper()
        entry = output.setdefault(strategy, {"realized_pnl": 0.0, "finalized_bundles": 0.0, "live_bundles": 0.0})
        entry["live_bundles"] += 1.0
    return output


class V6Collector(V4Collector):
    def collect(self) -> str:
        base = super().collect()
        metrics = Metrics()
        now = time.time()
        status_path = self.run_root / "runtime_status.json"
        status = _read_json(status_path) or {}
        allocator = _read_json(self.run_root / "allocator_status.json") or {}
        cfg = _read_json(self.config_path) or {}
        v6 = cfg.get("v6") if isinstance(cfg.get("v6"), dict) else {}
        starting = _float(cfg.get("starting_capital"), 10000.0)
        strategy_rows = {
            str(row.get("name") or ""): row
            for row in _read_csv(self.run_root / "strategy_status.csv")
            if str(row.get("name") or "")
        }

        relations = status.get("relations") if isinstance(status.get("relations"), dict) else {}
        graph_research = status.get("graph_research") if isinstance(status.get("graph_research"), dict) else {}
        if not graph_research:
            graph_research = _read_json(self.run_root / "graph_research_status.json") or {}
        relation_guard = _read_json(self.run_root / "relation_guard_status.json") or {}
        queue_filter = _read_json(self.run_root / "queue_filter_status.json") or {}
        local_factor = status.get("local_factor") if isinstance(status.get("local_factor"), dict) else {}
        typed_structural = _read_json(self.run_root / "typed_structural_status.json") or {}
        bridge = status.get("external_bridge") if isinstance(status.get("external_bridge"), dict) else {}
        evidence_path = self.run_root / "v7_execution_evidence.json"
        evidence = _read_json(evidence_path) or {}
        evidence_models = evidence.get("models") if isinstance(evidence.get("models"), dict) else {}
        evidence_age = max(0.0, now - (_mtime(evidence_path) or now)) if evidence else 1e12

        maker = _last_csv_row(self.run_root / "maker" / "maker_equity.csv") or {}
        micro = _read_json(self.run_root / "micro_taker" / "status.json") or {}
        exploration = micro.get("exploration") if isinstance(micro.get("exploration"), dict) else {}
        broker = _last_csv_row(self.run_root / "multileg_equity.csv") or {}
        hard = _read_json(self.run_root / "hard_arb" / "status.json") or {}
        external = _read_json(self.run_root / "external" / "status.json") or {}
        maker_order_log = _read_csv(self.run_root / "maker" / "maker_order_log.csv")
        broker_events = _read_csv(self.run_root / "multileg_events.csv")
        open_orders = _open_orders(self.run_root)
        recent_fills = _recent_fills(self.run_root)
        rv_breakdown = _rv_strategy_breakdown(self.run_root)

        durable_fills = {
            "micro_maker": _fill_counts(self.run_root / "maker" / "maker_fills.csv"),
            "micro_taker": _fill_counts(self.run_root / "micro_taker" / "fills.csv"),
            "relative_value": _multileg_fill_counts(self.run_root / "multileg_events.csv"),
            "graph_hard": _fill_counts(self.run_root / "hard_arb" / "fills.csv"),
            "external": _fill_counts(self.run_root / "external" / "fills.csv"),
        }
        fractions = {
            "micro_maker": _float(v6.get("micro_maker_capital_fraction"), 0.12),
            "micro_taker": _float(v6.get("micro_taker_capital_fraction"), 0.08),
            "relative_value": _float(v6.get("relative_value_capital_fraction"), 0.50),
            "graph_hard": _float(v6.get("hard_arb_capital_fraction"), 0.15),
            "external": _float(v6.get("external_capital_fraction"), 0.10),
        }
        maker_equity = _float(maker.get("equity"), starting * fractions["micro_maker"])
        maker_cash = _float(maker.get("cash"), starting * fractions["micro_maker"])
        enrichment = {
            "micro_maker": {
                "capital_fraction": fractions["micro_maker"],
                "starting_capital": starting * fractions["micro_maker"],
                "cash": maker_cash,
                "gross_exposure": _float(maker.get("reserved_cash")) + max(0.0, maker_equity - maker_cash),
                "drawdown": _float(maker.get("drawdown")),
                "realized_pnl": _maker_realized_pnl(self.run_root / "maker" / "maker_fills.csv"),
                "orders_total": sum(1 for row in maker_order_log if str(row.get("action") or "").upper() == "POST"),
            },
            "micro_taker": {
                "capital_fraction": fractions["micro_taker"],
                "starting_capital": starting * fractions["micro_taker"],
                "cash": _float(micro.get("cash"), starting * fractions["micro_taker"]),
                "gross_exposure": _float(micro.get("gross_exposure")),
                "drawdown": _float(micro.get("drawdown")),
                "realized_pnl": _float(micro.get("realized_pnl")),
                "orders_total": durable_fills["micro_taker"]["fills"],
            },
            "relative_value": {
                "capital_fraction": fractions["relative_value"],
                "starting_capital": starting * fractions["relative_value"],
                "cash": _float(broker.get("cash"), starting * fractions["relative_value"]),
                "gross_exposure": _float(broker.get("gross_entry_cash")) + _float(broker.get("reserved_cash")),
                "drawdown": _float(broker.get("drawdown")),
                "realized_pnl": _sum_csv(self.run_root / "bundle_ledger.csv", "net_pnl"),
                # Raw Graph scanner spreads are research diagnostics, not
                # executable Relative Value signals or edges.
                "signals": _float(local_factor.get("bundles")),
                "best_edge": _float(local_factor.get("best_edge")),
                "orders_total": sum(1 for row in broker_events if str(row.get("event") or "").upper() == "POST"),
            },
            "graph_hard": {
                "capital_fraction": fractions["graph_hard"],
                "starting_capital": starting * fractions["graph_hard"],
                "cash": _float(hard.get("cash"), starting * fractions["graph_hard"]),
                "gross_exposure": _float(hard.get("gross_exposure")),
                "drawdown": _float(hard.get("drawdown")),
                "realized_pnl": _float(hard.get("realized_pnl")),
                "orders_total": durable_fills["graph_hard"]["buy_fills"],
            },
            "external": {
                "capital_fraction": fractions["external"],
                "starting_capital": starting * fractions["external"],
                "cash": _float(external.get("cash"), starting * fractions["external"]),
                "gross_exposure": _float(external.get("gross_exposure")),
                "drawdown": _float(external.get("drawdown")),
                "realized_pnl": _float(external.get("realized_pnl")),
                "orders_total": 0,
            },
        }

        metrics.sample(
            "polymarket_v6_exporter_info",
            1,
            help_text="Static V6 model-specific exporter metadata.",
            labels={"version": EXPORTER_V6_VERSION},
        )
        metrics.sample("polymarket_v6_open_orders", len(open_orders), help_text="Current V6 paper open orders across maker and relative-value sleeves.")
        metrics.sample("polymarket_v6_fills_total", sum(row["fills"] for row in durable_fills.values()), help_text="Durable V6 paper fill count across model sleeves.")
        metrics.sample("polymarket_v6_realized_pnl_usd", sum(_float(row.get("realized_pnl")) for row in enrichment.values()), help_text="Aggregate realized V6 paper PnL from durable ledgers.")

        for name, raw_row in (status.get("strategies") or {}).items():
            if not isinstance(raw_row, dict):
                continue
            row = dict(raw_row)
            row.update(enrichment.get(str(name), {}))
            row.update(durable_fills.get(str(name), {}))
            sleeve_alive, sleeve_age = _model_health(strategy_rows, str(name))
            alert_staleness = 0.0 if sleeve_age <= V6_MODEL_FRESH_SECONDS else sleeve_age
            labels = {"model": str(name), "expert": str(name)}
            fields = {
                "polymarket_model_info": (1.0, "V6 independent economic model metadata."),
                "polymarket_model_capital_fraction": (_float(row.get("capital_fraction")), "Fraction of V6 parent paper capital allocated to the model."),
                "polymarket_model_starting_capital_usd": (_float(row.get("starting_capital")), "Initial V6 paper capital allocated to the model."),
                "polymarket_model_cash_usd": (_float(row.get("cash")), "Current V6 paper cash by model."),
                "polymarket_model_equity_usd": (_float(row.get("equity")), "V6 paper equity by model."),
                "polymarket_model_pnl_usd": (_float(row.get("pnl")), "V6 paper PnL by model."),
                "polymarket_model_realized_pnl_usd": (_float(row.get("realized_pnl")), "V6 realized paper PnL by model where a durable realized ledger is available."),
                "polymarket_model_gross_exposure_usd": (_float(row.get("gross_exposure")), "V6 committed/executed gross exposure by model."),
                "polymarket_model_drawdown_ratio": (_float(row.get("drawdown")), "V6 model-local paper drawdown."),
                "polymarket_model_open_positions": (_float(row.get("live_units")), "V6 live orders, bundles, or positions by model."),
                "polymarket_model_orders_total": (_float(row.get("orders_total")), "Durable V6 paper orders posted or entries attempted by model."),
                "polymarket_model_kill_switch": (1.0 if row.get("killed") else 0.0, "V6 model-local kill state."),
                "polymarket_model_alive": (sleeve_alive, "Whether the corresponding V6 strategy-status sleeve is alive."),
                "polymarket_model_status_age_seconds": (sleeve_age, "Age reported by the corresponding V6 strategy-status sleeve."),
                "polymarket_model_alert_staleness_seconds": (alert_staleness, "V6 model staleness used by alerts after a bounded two-report grace window."),
                "polymarket_model_startup_grace_active": (1.0 if sleeve_age <= V6_MODEL_FRESH_SECONDS else 0.0, "Compatibility health flag: V6 sleeve is inside the bounded freshness window."),
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
                    help_text="Cumulative V6 simulated fill events by model and action, sourced directly from durable execution ledgers.",
                    metric_type="counter",
                    labels={**labels, "action": action},
                )

            # Dedicated aliases retained for V6 consumers introduced before the
            # stable action-labelled Grafana contract was restored.
            metrics.sample("polymarket_model_buy_fills_total", _float(row.get("buy_fills")), help_text="Cumulative V6 entry/buy fill events by model.", metric_type="counter", labels=labels)
            metrics.sample("polymarket_model_sell_fills_total", _float(row.get("sell_fills")), help_text="Cumulative V6 exit/sell fill events by model.", metric_type="counter", labels=labels)
            metrics.sample("polymarket_model_settle_fills_total", _float(row.get("settle_fills")), help_text="Cumulative V6 settlement fill events by model.", metric_type="counter", labels=labels)

            # V7 is a fail-closed evidence sidecar. Its target label is fixed by
            # policy and makes it impossible to misread terminal forecasts as
            # short-horizon execution alpha in Grafana.
            evidence_row = evidence_models.get(str(name)) if isinstance(evidence_models.get(str(name)), dict) else {}
            evidence_labels = {
                **labels,
                "target": str(evidence_row.get("target") or "unavailable"),
                "state": str(evidence_row.get("state") or "UNAVAILABLE"),
            }
            metrics.sample(
                "polymarket_model_execution_evidence_present",
                1.0 if evidence_row else 0.0,
                help_text="Whether a typed V7 execution-evidence record exists for this model.",
                labels=evidence_labels,
            )
            metrics.sample(
                "polymarket_model_execution_evidence_eligible",
                1.0 if evidence_row.get("paper_eligible") is True else 0.0,
                help_text="Whether the model passes all V7 paper evidence gates; this does not mutate allocation.",
                labels=evidence_labels,
            )
            metrics.sample(
                "polymarket_model_execution_evidence_fills",
                _float(evidence_row.get("fills")),
                help_text="V7 fill observations used by the typed execution-evidence gate.",
                labels=evidence_labels,
            )
            metrics.sample(
                "polymarket_model_execution_evidence_pnl_observations",
                _float(evidence_row.get("realized_pnl_observations")),
                help_text="V7 realized PnL observations used by the execution-evidence gate.",
                labels=evidence_labels,
            )
            metrics.sample(
                "polymarket_model_execution_evidence_markout_observations",
                _float(evidence_row.get("forward_markout_observations")),
                help_text="V7 forward-markout observations used by the execution-evidence gate.",
                labels=evidence_labels,
            )
            metrics.sample(
                "polymarket_model_execution_evidence_net_pnl_usd",
                _float(evidence_row.get("net_pnl")),
                help_text="V7 realized paper net PnL for the model evidence window.",
                labels=evidence_labels,
            )
            metrics.sample(
                "polymarket_model_execution_evidence_stressed_net_pnl_usd",
                _float(evidence_row.get("stressed_net_pnl")),
                help_text="V7 1.5x-cost-stressed paper net PnL for the model evidence window.",
                labels=evidence_labels,
            )
            metrics.sample(
                "polymarket_model_execution_evidence_bootstrap_pvalue",
                _float(evidence_row.get("bootstrap_one_sided_pvalue"), 1.0),
                help_text="V7 one-sided day-block bootstrap p-value for model PnL.",
                labels=evidence_labels,
            )
            metrics.sample(
                "polymarket_model_execution_evidence_terminal_calibration_observations",
                _float(evidence_row.get("terminal_calibration_observations")),
                help_text="Resolved terminal labels used to assess Brier improvement over the market.",
                labels=evidence_labels,
            )
            metrics.sample(
                "polymarket_model_execution_evidence_brier_improvement",
                _float(evidence_row.get("brier_improvement_over_market")),
                help_text="Mean Brier-score improvement over the contemporaneous market probability.",
                labels=evidence_labels,
            )
            metrics.sample(
                "polymarket_model_execution_evidence_age_seconds",
                evidence_age,
                help_text="Age of the V7 execution-evidence report.",
                labels=evidence_labels,
            )

        for strategy, row in sorted(rv_breakdown.items()):
            labels = {"strategy": strategy}
            metrics.sample("polymarket_strategy_realized_pnl_usd", _float(row.get("realized_pnl")), help_text="Realized multileg PnL by V6 strategy.", labels=labels)
            metrics.sample("polymarket_strategy_finalized_bundles_total", _float(row.get("finalized_bundles")), help_text="Finalized multileg bundles by V6 strategy.", labels=labels)
            metrics.sample("polymarket_strategy_live_bundles", _float(row.get("live_bundles")), help_text="Live multileg bundles by V6 strategy.", labels=labels)

        for order in open_orders:
            labels = {
                "model": _label(order.get("model"), 32),
                "strategy": _label(order.get("strategy"), 48),
                "bundle_id": _label(order.get("bundle_id"), 80),
                "market_id": _label(order.get("market_id"), 80),
                "side": _label(order.get("side"), 8),
                "state": _label(order.get("state"), 24),
                "limit_price": f"{_float(order.get('limit_price')):.6g}",
            }
            metrics.sample("polymarket_open_order_remaining_shares", _float(order.get("remaining_shares")), help_text="Current individual V6 paper order remaining shares.", labels=labels)
            metrics.sample("polymarket_open_order_queue_ahead_shares", _float(order.get("queue_ahead")), help_text="Current conservative queue ahead for an individual V6 paper order.", labels=labels)

        for fill in recent_fills:
            labels = {
                "model": _label(fill.get("model"), 32),
                "strategy": _label(fill.get("strategy"), 48),
                "market_id": _label(fill.get("market_id"), 80),
                "side": _label(fill.get("side"), 8),
                "action": _label(fill.get("action"), 32),
                "timestamp": str(int(_float(fill.get("timestamp")))),
                "price": f"{_float(fill.get('price')):.6g}",
                "shares": f"{_float(fill.get('shares')):.6g}",
            }
            metrics.sample("polymarket_recent_fill_pnl_usd", _float(fill.get("pnl")), help_text="Recent individual V6 paper fill PnL when realized on that row.", labels=labels)

        metrics.sample("polymarket_v6_relation_bundles", _float(relations.get("bundles")), help_text="Current executable graph/structural V6 bundles.")
        metrics.sample("polymarket_v6_relation_best_edge_ratio", _float(relations.get("best_edge")), help_text="Raw graph/structural scanner spread; it is not a GRAPH_RV execution edge.")
        metrics.sample("polymarket_v6_relation_guard_accepted_rows", _float(relation_guard.get("accepted_rows")), help_text="Guarded Graph relation intent rows; one basket can contain multiple rows.")
        metrics.sample("polymarket_v6_relation_guard_accepted_bundles", _unique_bundle_count(self.run_root / "relation_intents.csv"), help_text="Unique Graph relation bundles after contract-semantic guard, before research-only joint-fill EV measurement.")
        joint_models = graph_research.get("joint_models") if isinstance(graph_research.get("joint_models"), list) else []
        joint_observations = sum(
            _float(row.get("observations")) for row in joint_models if isinstance(row, dict)
        )
        graph_labels = {
            "mode": _label(graph_research.get("graph_mode") or "UNAVAILABLE", 32),
            "broker_routing_enabled": str(int(bool(graph_research.get("broker_routing_enabled", False)))),
        }
        metrics.sample("polymarket_v6_graph_research_info", 1.0 if graph_research else 0.0, help_text="GRAPH_RV research mode and broker-routing state; a value of zero routing is intentional.", labels=graph_labels)
        metrics.sample("polymarket_v6_graph_research_candidate_bundles", _float(graph_research.get("candidate_bundles")), help_text="Current GRAPH_RV baskets measured in research, never broker-routed by this path.")
        metrics.sample("polymarket_v6_graph_research_economic_candidates", _float(graph_research.get("economic_research_candidates")), help_text="GRAPH_RV baskets with positive conservative joint-fill EV, still research-only.")
        metrics.sample("polymarket_v6_graph_research_insufficient_evidence", _float(graph_research.get("insufficient_evidence_candidates")), help_text="GRAPH_RV baskets lacking enough empirical joint full/partial/zero fill evidence.")
        metrics.sample("polymarket_v6_graph_research_joint_observations", joint_observations, help_text="Finalized paper GRAPH_RV basket observations used for joint-fill models.")
        metrics.sample("polymarket_v6_graph_research_broker_routing_enabled", 1.0 if graph_research.get("broker_routing_enabled") is True else 0.0, help_text="Whether GRAPH_RV research is allowed to write broker intents; must remain zero in the research configuration.")
        metrics.sample("polymarket_v6_graph_research_raw_edge_is_execution_edge", 1.0 if graph_research.get("raw_scanner_edge_is_execution_edge") is True else 0.0, help_text="Whether a raw GRAPH_RV scanner spread is treated as execution edge; must remain zero.")
        metrics.sample("polymarket_v6_queue_filter_accepted_bundles", _float(queue_filter.get("accepted_bundles")), help_text="Fill-aware maker bundles admitted to broker.")
        metrics.sample("polymarket_v6_queue_filter_improved_bundles", _float(queue_filter.get("improved_bundles")), help_text="Maker bundles whose quote was improved using edge budget.")
        metrics.sample("polymarket_v6_queue_filter_best_joint_fill_probability", _float(queue_filter.get("best_joint_fill_probability")), help_text="Best current joint fill probability proxy.")
        metrics.sample("polymarket_v6_queue_filter_best_expected_fill_edge_ratio", _float(queue_filter.get("best_expected_fill_edge")), help_text="Best edge times joint-fill proxy.")
        metrics.sample("polymarket_v6_queue_filter_max_queue_ahead_shares", _float(queue_filter.get("max_queue_ahead")), help_text="Largest queue ahead observed by V6 queue filter.")
        metrics.sample("polymarket_v6_local_factor_bundles", _float(local_factor.get("bundles")), help_text="Current local-factor bundles.")
        metrics.sample("polymarket_v6_local_factor_clusters", _float(local_factor.get("clusters")), help_text="Current local factor clusters evaluated.")
        metrics.sample("polymarket_v6_local_factor_candidates", _float(local_factor.get("reversion_tests")), help_text="Local factor residual reversion tests before FDR.")
        metrics.sample("polymarket_v6_local_factor_fdr_survivors", _float(local_factor.get("fdr_eligible_signals")), help_text="Local factor block-bootstrap FDR-eligible signals.")
        metrics.sample("polymarket_v6_typed_structural_bundles", _float(typed_structural.get("bundles")), help_text="Fail-closed typed structural bundles.")
        metrics.sample("polymarket_v6_external_signals", _float(bridge.get("materialized_signals")), help_text="OOS-approved external probabilities materialized for V6.")
        metrics.sample("polymarket_v6_micro_taker_exploration_enabled", 1.0 if exploration.get("enabled") is True else 0.0, help_text="Whether the bounded paper taker exploration sleeve is enabled.")
        metrics.sample("polymarket_v6_micro_taker_exploration_active_positions", _float(exploration.get("active_positions")), help_text="Current bounded paper exploration taker positions.")
        metrics.sample("polymarket_v6_micro_taker_exploration_hourly_opens", _float(exploration.get("hourly_opens")), help_text="Exploration entries opened in the rolling one-hour stratified budget.")
        metrics.sample("polymarket_v6_micro_taker_exploration_opened_last_tick", _float(exploration.get("opened_last_tick")), help_text="Exploration taker entries opened in the last micro tick.")
        metrics.sample("polymarket_v6_micro_taker_exploration_candidate_strata", _float(exploration.get("candidate_strata_last_tick")), help_text="Eligible activity/depth taker strata seen in the last tick.")
        metrics.sample("polymarket_v6_micro_taker_exploration_depth_rejections", _float(exploration.get("depth_rejections_last_tick")), help_text="Exploration candidates rejected by executable depth and minimum-size checks.")
        metrics.sample("polymarket_v6_micro_taker_exploration_realized_pnl_usd", _float(exploration.get("realized_pnl_total")), help_text="Cumulative realized paper PnL for exploration only; never alpha-promotion evidence.")
        metrics.sample("polymarket_v6_scrape_timestamp_seconds", now, help_text="V6 exporter scrape timestamp.")

        # Transitional metrics consumed by stable monitoring/server health checks.
        # They describe V6 only; no V5 allocator/expert execution is restored.
        metrics.sample("polymarket_allocator_state_present", 1 if allocator else 0, help_text="Legacy health compatibility view for V6.")
        metrics.sample("polymarket_allocator_models_expected", _float(allocator.get("models_expected"), 5), help_text="V6 model count exposed through stable health metric.")
        metrics.sample("polymarket_allocator_models_alive", _float(allocator.get("models_alive")), help_text="V6 alive model count reported by the per-sleeve allocator health ledger.")
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
