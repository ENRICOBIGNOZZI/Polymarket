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

EXPORTER_V6_VERSION = "1.5.0"
V6_MODEL_FRESH_SECONDS = 120.0


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
        status_age = max(0.0, now - (_mtime(status_path) or now)) if status else 1e12
        model_alive = 1.0 if status and status_age <= V6_MODEL_FRESH_SECONDS else 0.0
        alert_staleness = 0.0 if status_age <= V6_MODEL_FRESH_SECONDS else status_age

        relations = status.get("relations") if isinstance(status.get("relations"), dict) else {}
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
                "signals": _float(relations.get("bundles")) + _float(local_factor.get("bundles")),
                "best_edge": max(_float(relations.get("best_edge")), _float(local_factor.get("best_edge"))),
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
        metrics.sample("polymarket_v6_relation_best_edge_ratio", _float(relations.get("best_edge")), help_text="Best current graph/structural maker edge.")
        metrics.sample("polymarket_v6_relation_guard_accepted_bundles", _float(relation_guard.get("accepted_bundles")), help_text="Graph relation bundles after economic guard.")
        metrics.sample("polymarket_v6_queue_filter_accepted_bundles", _float(queue_filter.get("accepted_bundles")), help_text="Fill-aware maker bundles admitted to broker.")
        metrics.sample("polymarket_v6_queue_filter_improved_bundles", _float(queue_filter.get("improved_bundles")), help_text="Maker bundles whose quote was improved using edge budget.")
        metrics.sample("polymarket_v6_queue_filter_best_joint_fill_probability", _float(queue_filter.get("best_joint_fill_probability")), help_text="Best current joint fill probability proxy.")
        metrics.sample("polymarket_v6_queue_filter_best_expected_fill_edge_ratio", _float(queue_filter.get("best_expected_fill_edge")), help_text="Best edge times joint-fill proxy.")
        metrics.sample("polymarket_v6_queue_filter_max_queue_ahead_shares", _float(queue_filter.get("max_queue_ahead")), help_text="Largest queue ahead observed by V6 queue filter.")
        metrics.sample("polymarket_v6_local_factor_bundles", _float(local_factor.get("bundles")), help_text="Current local-factor bundles.")
        metrics.sample("polymarket_v6_local_factor_clusters", _float(local_factor.get("clusters")), help_text="Current local factor clusters evaluated.")
        metrics.sample("polymarket_v6_local_factor_candidates", _float(local_factor.get("candidate_count")), help_text="Local factor residual candidates before FDR.")
        metrics.sample("polymarket_v6_local_factor_fdr_survivors", _float(local_factor.get("fdr_survivors")), help_text="Local factor block-bootstrap FDR survivors.")
        metrics.sample("polymarket_v6_typed_structural_bundles", _float(typed_structural.get("bundles")), help_text="Fail-closed typed structural bundles.")
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