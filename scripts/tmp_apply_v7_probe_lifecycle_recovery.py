#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"missing patch anchor in {path}: {old[:160]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


# A five-share Polymarket minimum at approximately 50c cannot fit inside the
# former USD 2 ceiling. Keep the probe tiny, but make it physically executable.
config_path = Path("config/v7_external_fair.json")
config = json.loads(config_path.read_text(encoding="utf-8"))
probe = config["paper_exploration_probe"]
probe.update({
    "max_capital_fraction": 0.0025,
    "max_notional_usd": 5.0,
    "max_loss_usd": 5.0,
    "require_minimum_order_feasible": True,
})
config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

# The opportunity parser remains the independent, fail-closed numeric firewall.
replace_once(
    "scripts/v7_opportunity.py",
    "or loss_cap > 2.0 + 1e-9",
    "or loss_cap > 5.0 + 1e-9",
)

router_path = "scripts/v7_external_fair_paper_router.py"
replace_once(
    router_path,
    '        "require_arrival_revalidation": True,\n        "promotion_credit": False,',
    '        "require_arrival_revalidation": True,\n        "require_minimum_order_feasible": True,\n        "promotion_credit": False,',
)
replace_once(
    router_path,
    '        "max_capital_fraction": (0.00001, 0.0005),\n        "max_notional_usd": (0.25, 2.0),\n        "max_loss_usd": (0.25, 2.0),',
    '        "max_capital_fraction": (0.00001, 0.0025),\n        "max_notional_usd": (0.25, 5.0),\n        "max_loss_usd": (0.25, 5.0),',
)

# Explain the first failing probe gate instead of collapsing every cold-start
# abstention into the economically useless NO_ROBUST_EV label.
replace_once(
    router_path,
    '''    return sorted(rows, key=lambda row: (-row["point_ev"], row["outcome"]))


def executable_sell_value''',
    '''    return sorted(rows, key=lambda row: (-row["point_ev"], row["outcome"]))


def paper_probe_diagnostics(
    status: dict[str, Any], books: dict[str, Book], policy: dict[str, Any],
    probe_policy: dict[str, Any] | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "enabled": probe_policy is not None,
        "eligible": False,
        "reason": "PAPER_PROBE_DISABLED" if probe_policy is None else "PAPER_PROBE_UNASSESSED",
        "tte_seconds": None,
        "model_market_disagreement": None,
        "best_point_ev_per_share": None,
        "minimum_point_ev_per_share": (
            probe_policy.get("minimum_point_ev_per_share") if probe_policy else None
        ),
    }
    if probe_policy is None:
        return result
    fair = status.get("fair") if isinstance(status.get("fair"), dict) else {}
    result["probability_model_id"] = fair.get("probability_model_id")
    if (
        fair.get("valid") is not True
        or fair.get("paper_exploration_bootstrap") is not True
        or fair.get("promotion_eligible") is not False
        or fair.get("real_money_authority") is not False
        or fair.get("probability_model_id") != probe_policy["required_probability_model_id"]
        or not identity_hash(fair.get("probability_model_hash"))
    ):
        result["reason"] = "PAPER_PROBE_BOOTSTRAP_FAIR_UNAVAILABLE"
        return result
    contract = status.get("contract") if isinstance(status.get("contract"), dict) else {}
    reference = status.get("settlement_reference") if isinstance(status.get("settlement_reference"), dict) else {}
    oracle = status.get("oracle") if isinstance(status.get("oracle"), dict) else {}
    external = status.get("external") if isinstance(status.get("external"), dict) else {}
    market = status.get("market") if isinstance(status.get("market"), dict) else {}
    if not (
        status.get("paper_only") is True
        and status.get("authenticated_execution") is False
        and status.get("real_order_submission") is False
        and contract.get("verified") is True
        and contract.get("rules_hash_recognized") is True
        and reference.get("valid") is True
        and oracle.get("healthy") is True
        and oracle.get("continuity") != "CONTINUITY_UNKNOWN"
        and external.get("healthy") is True
    ):
        result["reason"] = "PAPER_PROBE_INFRASTRUCTURE_INELIGIBLE"
        return result
    tte = finite(fair.get("tte_seconds"), math.nan)
    result["tte_seconds"] = tte if math.isfinite(tte) else None
    if not probe_policy["minimum_tte_seconds"] <= tte <= probe_policy["maximum_tte_seconds"]:
        result["reason"] = "PAPER_PROBE_TTE_OUTSIDE_WINDOW"
        return result
    bucket = tte_policy(fair, policy)
    if bucket and bucket.get("action") != "TAKER_SHADOW":
        result["reason"] = "PAPER_PROBE_TTE_POLICY_DISABLED"
        return result
    calculated = int(fair.get("calculated_monotonic_ns") or 0)
    valid_until = int(fair.get("valid_until_monotonic_ns") or 0)
    current = time.monotonic_ns()
    if calculated <= 0 or calculated > current or valid_until < current:
        result["reason"] = "PAPER_PROBE_FAIR_EXPIRED"
        return result
    market_yes = live_market_yes(books, market)
    fair_yes = finite(fair.get("yes"), math.nan)
    result["market_yes"] = market_yes
    result["fair_yes"] = fair_yes if math.isfinite(fair_yes) else None
    if market_yes is None or not math.isfinite(fair_yes):
        result["reason"] = "PAPER_PROBE_LIVE_MARKET_UNAVAILABLE"
        return result
    disagreement = abs(fair_yes - market_yes)
    result["model_market_disagreement"] = disagreement
    if not probe_policy["minimum_model_market_disagreement"] <= disagreement <= probe_policy["maximum_model_market_disagreement"]:
        result["reason"] = "PAPER_PROBE_DISAGREEMENT_OUTSIDE_WINDOW"
        return result
    schedule = market.get("fee_schedule") if isinstance(market.get("fee_schedule"), dict) else {}
    execution_risk = float(bucket.get(
        "execution_risk_per_share", policy.get("base_execution_risk_per_share", 0.0005)
    ))
    point_evs: dict[str, float] = {}
    for outcome, token, point_probability in (
        ("YES", str(market.get("yes_token") or ""), fair_yes),
        ("NO", str(market.get("no_token") or ""), 1.0 - fair_yes),
    ):
        book = books.get(token)
        if book is None or not book.asks:
            continue
        ask = book.asks[0][0]
        value = point_probability - ask - fee_per_share(ask, schedule) - execution_risk
        if math.isfinite(value):
            point_evs[outcome] = value
    result["point_evs_per_share"] = point_evs
    if not point_evs:
        result["reason"] = "PAPER_PROBE_BOOK_UNAVAILABLE"
        return result
    result["best_point_ev_per_share"] = max(point_evs.values())
    candidates = paper_probe_candidates(status, books, policy, probe_policy)
    if candidates:
        result["eligible"] = True
        result["reason"] = "PAPER_PROBE_CANDIDATE_READY"
        result["candidate_count"] = len(candidates)
        return result
    result["reason"] = "PAPER_PROBE_POINT_EV_BELOW_THRESHOLD"
    return result


def executable_sell_value''',
)

# Persist the diagnostics required to distinguish model, market, sizing and
# coordinator failures.
replace_once(
    router_path,
    '            "last_decision": {},\n        }',
    '            "last_decision": {},\n            "last_probe_diagnostics": {}, "last_probe_size_diagnostics": {},\n        }',
)

# Restore the exact coordinator receipt and PAPER authority after a process
# restart; without this, terminal events are quarantined by the single writer.
replace_once(
    router_path,
    '                "order_id": str(row.get("counterfactual_id") or ""),',
    '                "order_id": str(row.get("order_id") or row.get("counterfactual_id") or ""),',
)
replace_once(
    router_path,
    '''                "market_mid_source": metadata.get("market_mid_source"),
            }''',
    '''                "market_mid_source": metadata.get("market_mid_source"),
                "coordinator_receipt": metadata.get("coordinator_receipt"),
                "paper_exploration": metadata.get("paper_exploration") is True,
                "paper_bootstrap_probe": metadata.get("paper_bootstrap_probe") is True,
                "canonical_metadata": (
                    metadata.get("canonical_metadata")
                    if isinstance(metadata.get("canonical_metadata"), dict)
                    else {
                        "coordinator_receipt": metadata.get("coordinator_receipt"),
                        "paper_exploration": metadata.get("paper_exploration") is True,
                        "paper_bootstrap_probe": metadata.get("paper_bootstrap_probe") is True,
                        "economic_authority": metadata.get("economic_authority"),
                        "counterfactual": False,
                        "excluded_from_portfolio_equity": False,
                        "research_evidence_only": False,
                    }
                ),
            }''',
)
replace_once(
    router_path,
    '''        self.state["counterfactual_fills"] = len(fills)
        self.state["counterfactual_realized_pnl"] = sum(''',
    '''        self.state["counterfactual_fills"] = len(fills)
        self.state["orders"] = len(fills)
        self.state["fills"] = len(fills)
        self.state["probe_fills"] = sum(
            int((row.get("metadata") or {}).get("paper_bootstrap_probe") is True)
            for row in fills
        )
        self.state["counterfactual_realized_pnl"] = sum(''',
)

# Size a real exchange minimum, not an abstract fraction that rounds to zero.
old_order_size = '''    def order_size(self, row: dict[str, Any]) -> float:
        book: Book = row["book"]
        ask = float(row["ask"])
        fee = float(row["fee_per_share"])
        execution_risk = float(row["execution_risk"])
        max_depth_fraction = min(1.0, max(0.0, float(self.policy.get("max_depth_fraction", 0.5))))
        depth_survival = min(1.0, max(0.0, float(self.policy.get("depth_survival_fraction", 0.75))))
        depth_fraction = min(max_depth_fraction, depth_survival)
        visible = book.asks[0][1] if book.asks else 0.0
        if row.get("paper_bootstrap_probe") is True:
            if self.probe_policy is None:
                return 0.0
            available_notional = min(
                float(self.state.get("starting_capital") or 0.0)
                * float(self.probe_policy["max_capital_fraction"]),
                float(self.probe_policy["max_notional_usd"]),
                float(self.probe_policy["max_loss_usd"]),
            )
        else:
            fraction_key = "max_market_capital_fraction" if self.model_mature else "immature_exploration_capital_fraction"
            fraction = float(self.policy.get(fraction_key, 0.02 if self.model_mature else 0.0025))
            available_notional = max(0.0, float(self.state["starting_capital"]) * fraction)
        unit_budget_cost = max(1e-9, ask + fee + execution_risk)
        size = min(visible * depth_fraction, available_notional / unit_budget_cost)
        size = math.floor(size * 100.0 + 1e-9) / 100.0
        if size + 1e-9 < book.min_order_size:
            return 0.0
        return size
'''
new_order_size = '''    def order_size(self, row: dict[str, Any]) -> float:
        book: Book = row["book"]
        ask = float(row["ask"])
        fee = float(row["fee_per_share"])
        execution_risk = float(row["execution_risk"])
        max_depth_fraction = min(1.0, max(0.0, float(self.policy.get("max_depth_fraction", 0.5))))
        depth_survival = min(1.0, max(0.0, float(self.policy.get("depth_survival_fraction", 0.75))))
        depth_fraction = min(max_depth_fraction, depth_survival)
        visible = book.asks[0][1] if book.asks else 0.0
        is_probe = row.get("paper_bootstrap_probe") is True
        if is_probe:
            if self.probe_policy is None:
                self.state["last_probe_size_diagnostics"] = {
                    "feasible": False, "reason": "PAPER_PROBE_DISABLED",
                }
                return 0.0
            available_notional = min(
                float(self.state.get("starting_capital") or 0.0)
                * float(self.probe_policy["max_capital_fraction"]),
                float(self.probe_policy["max_notional_usd"]),
                float(self.probe_policy["max_loss_usd"]),
            )
        else:
            fraction_key = "max_market_capital_fraction" if self.model_mature else "immature_exploration_capital_fraction"
            fraction = float(self.policy.get(fraction_key, 0.02 if self.model_mature else 0.0025))
            available_notional = max(0.0, float(self.state["starting_capital"]) * fraction)
        unit_budget_cost = max(1e-9, ask + fee + execution_risk)
        raw_size = min(visible * depth_fraction, available_notional / unit_budget_cost)
        size = math.floor(raw_size * 100.0 + 1e-9) / 100.0
        if is_probe:
            minimum_required_loss = book.min_order_size * unit_budget_cost
            if visible * depth_fraction + 1e-9 < book.min_order_size:
                reason = "PROBE_CONSERVATIVE_DEPTH_BELOW_MINIMUM"
            elif available_notional + 1e-9 < minimum_required_loss:
                reason = "PROBE_MINIMUM_ORDER_UNAFFORDABLE"
            elif size + 1e-9 < book.min_order_size:
                reason = "PROBE_MINIMUM_ORDER_UNEXECUTABLE"
            else:
                reason = "PROBE_SIZE_READY"
            self.state["last_probe_size_diagnostics"] = {
                "feasible": reason == "PROBE_SIZE_READY",
                "reason": reason,
                "capital_ceiling": available_notional,
                "minimum_required_loss": minimum_required_loss,
                "minimum_order_size": book.min_order_size,
                "visible_top_size": visible,
                "conservative_visible_size": visible * depth_fraction,
                "depth_fraction": depth_fraction,
                "unit_budget_cost": unit_budget_cost,
                "raw_size": raw_size,
                "rounded_size": size,
            }
        if size + 1e-9 < book.min_order_size:
            return 0.0
        return size
'''
replace_once(router_path, old_order_size, new_order_size)

replace_once(
    router_path,
    '''        size = self.order_size(row)
        if size <= 0.0:
            self.last_attempt_reason = "INVALID_SIZE"
            return False''',
    '''        size = self.order_size(row)
        if size <= 0.0:
            diagnostics = self.state.get("last_probe_size_diagnostics") if is_probe else {}
            self.last_attempt_reason = str(
                diagnostics.get("reason") if isinstance(diagnostics, dict) else ""
            ) or "INVALID_SIZE"
            return False''',
)

# Deterministic entry IDs allow a crash/restart retry to be deduplicated by the
# existing single-writer record-id cache.
replace_once(
    router_path,
    '''        spool_event(self.root, LedgerEvent(
            event_type="ORDER_SUBMITTED", strategy=STRATEGY, model_sha=self.sha,''',
    '''        spool_event(self.root, LedgerEvent(
            record_id=f"external-paper-order-{stable_id(order_id)}",
            event_type="ORDER_SUBMITTED", strategy=STRATEGY, model_sha=self.sha,''',
)
replace_once(
    router_path,
    '            limit_price=ask, intended_action="TAKE", intended_size=size, order_state="SUBMITTED_SHADOW",',
    '            limit_price=ask, intended_action="TAKE", intended_size=size, order_state="SUBMITTED_PAPER",',
)
replace_once(
    router_path,
    '''        spool_event(self.root, LedgerEvent(
            event_type="FILL", strategy=STRATEGY, model_sha=self.sha,''',
    '''        spool_event(self.root, LedgerEvent(
            record_id=f"external-paper-fill-{stable_id(fill_id)}",
            event_type="FILL", strategy=STRATEGY, model_sha=self.sha,''',
)

# Carry the receipt and authority through the durable VIRTUAL_FILL so a restart
# can later emit a valid MARKOUT/FINAL. Also persist the real order identity.
replace_once(
    router_path,
    '''            "VIRTUAL_FILL", counterfactual_id=counterfactual_id,
            strategy=STRATEGY, model_version=MODEL_VERSION,
            fill_id=fill_id, position_id=position_id, market_id=market_id,''',
    '''            "VIRTUAL_FILL", counterfactual_id=counterfactual_id,
            strategy=STRATEGY, model_version=MODEL_VERSION,
            order_id=order_id, fill_id=fill_id, position_id=position_id, market_id=market_id,''',
)
replace_once(
    router_path,
    '''                "arrival_model_market_disagreement": abs(
                    float((arrival_status.get("fair") or {}).get("yes"))
                    - float(arrival["market_yes"])
                ),
            },
        )
        self.state["counterfactual_fills"]''',
    '''                "arrival_model_market_disagreement": abs(
                    float((arrival_status.get("fair") or {}).get("yes"))
                    - float(arrival["market_yes"])
                ),
                "coordinator_receipt": receipt,
                "paper_exploration": True,
                "paper_bootstrap_probe": is_probe,
                "economic_authority": "PAPER_EXPLORATION",
                "counterfactual": False,
                "excluded_from_portfolio_equity": False,
                "research_evidence_only": False,
                "canonical_metadata": canonical_metadata,
            },
        )
        self.state["orders"] = int(self.state.get("orders") or 0) + 1
        self.state["fills"] = int(self.state.get("fills") or 0) + 1
        self.state["counterfactual_fills"]''',
)
replace_once(
    router_path,
    '''            "coordinator_receipt": receipt, "paper_exploration": True,
            "paper_bootstrap_probe": is_probe,
            "model_yes":''',
    '''            "coordinator_receipt": receipt, "paper_exploration": True,
            "paper_bootstrap_probe": is_probe,
            "canonical_metadata": canonical_metadata,
            "model_yes":''',
)

# Fail closed if a restored position lacks the coordinator receipt required by
# the canonical ledger firewall.
replace_once(
    router_path,
    '''        self.last_attempt_reason = "VIRTUAL_FILL"
        return True

    def observe_positions(self) -> None:''',
    '''        self.last_attempt_reason = "VIRTUAL_FILL"
        return True

    def canonical_position_metadata(self, position: dict[str, Any]) -> dict[str, Any]:
        metadata = position.get("canonical_metadata")
        if not isinstance(metadata, dict):
            metadata = {
                "coordinator_receipt": position.get("coordinator_receipt"),
                "paper_exploration": position.get("paper_exploration") is True,
                "paper_bootstrap_probe": position.get("paper_bootstrap_probe") is True,
                "economic_authority": "PAPER_EXPLORATION",
                "counterfactual": False,
                "excluded_from_portfolio_equity": False,
                "research_evidence_only": False,
            }
        receipt = metadata.get("coordinator_receipt")
        if not isinstance(receipt, dict):
            raise RuntimeError("PAPER_POSITION_COORDINATOR_RECEIPT_MISSING")
        if (
            metadata.get("paper_exploration") is not True
            or metadata.get("economic_authority") != "PAPER_EXPLORATION"
            or metadata.get("counterfactual") is not False
            or metadata.get("excluded_from_portfolio_equity") is not False
            or metadata.get("research_evidence_only") is not False
        ):
            raise RuntimeError("PAPER_POSITION_AUTHORITY_INVALID")
        return dict(metadata)

    def observe_positions(self) -> None:''',
)

# Canonical MARKOUT first, then the shadow mirror. A crash can therefore retry
# safely without losing the economically authoritative observation.
old_markout = '''                    for horizon in due:
                        self.emit_counterfactual(
                            "VIRTUAL_MARKOUT", strategy=STRATEGY, model_version=MODEL_VERSION,
                            counterfactual_id=str(position["counterfactual_id"]),
                            fill_id=str(position["fill_id"]), position_id=str(position["position_id"]),
                            market_id=str(position["market_id"]), event_id=str(position["event_id"]),
                            token_id=str(position["token_id"]), side="BUY", exchange_ts_ms=book.exchange_ts_ms,
                            receive_ts_ms=book.receive_ts_ms, book_snapshot_id=book.snapshot_id,
                            executable_liquidation_value=liquidation, markouts={f"{horizon}s": per_share},
                            metadata={"full_visible_depth": True, "fill_conditioned": True},
                        )
                        self.emit_shadow_ingress(LedgerEvent(
                            event_type="MARKOUT", strategy=STRATEGY, model_sha=self.sha,
                            model_version=MODEL_VERSION,
                            order_id=str(position["order_id"]), fill_id=str(position["fill_id"]),
                            position_id=str(position["position_id"]), market_id=str(position["market_id"]),
                            event_id=str(position["event_id"]), token_id=str(position["token_id"]),
                            side="BUY", exchange_ts_ms=book.exchange_ts_ms,
                            receive_ts_ms=book.receive_ts_ms, book_snapshot_id=book.snapshot_id,
                            executable_liquidation_value=liquidation,
                            markouts={f"{horizon}s": per_share},
                            metadata={"model_family": STRATEGY, "horizon_seconds": 300,
                                      "full_visible_depth": True, "fill_conditioned": True},
                        ))
                        position.setdefault("markouts", []).append(horizon)
'''
new_markout = '''                    canonical_metadata = self.canonical_position_metadata(position)
                    for horizon in due:
                        canonical_markout_metadata = {
                            **canonical_metadata,
                            "model_family": STRATEGY,
                            "horizon_seconds": 300,
                            "markout_horizon_seconds": horizon,
                            "full_visible_depth": True,
                            "fill_conditioned": True,
                        }
                        spool_event(self.root, LedgerEvent(
                            record_id=f"external-paper-markout-{stable_id(position['fill_id'], horizon, book.snapshot_id)}",
                            event_type="MARKOUT", strategy=STRATEGY, model_sha=self.sha,
                            model_version=MODEL_VERSION,
                            order_id=str(position["order_id"]), fill_id=str(position["fill_id"]),
                            position_id=str(position["position_id"]), market_id=str(position["market_id"]),
                            event_id=str(position["event_id"]), token_id=str(position["token_id"]),
                            side="BUY", exchange_ts_ms=book.exchange_ts_ms,
                            receive_ts_ms=book.receive_ts_ms, book_snapshot_id=book.snapshot_id,
                            executable_liquidation_value=liquidation,
                            markouts={f"{horizon}s": per_share},
                            metadata=canonical_markout_metadata,
                        ))
                        self.emit_counterfactual(
                            "VIRTUAL_MARKOUT", strategy=STRATEGY, model_version=MODEL_VERSION,
                            counterfactual_id=str(position["counterfactual_id"]),
                            fill_id=str(position["fill_id"]), position_id=str(position["position_id"]),
                            market_id=str(position["market_id"]), event_id=str(position["event_id"]),
                            token_id=str(position["token_id"]), side="BUY", exchange_ts_ms=book.exchange_ts_ms,
                            receive_ts_ms=book.receive_ts_ms, book_snapshot_id=book.snapshot_id,
                            executable_liquidation_value=liquidation, markouts={f"{horizon}s": per_share},
                            metadata={"full_visible_depth": True, "fill_conditioned": True},
                        )
                        self.emit_shadow_ingress(LedgerEvent(
                            event_type="MARKOUT", strategy=STRATEGY, model_sha=self.sha,
                            model_version=MODEL_VERSION,
                            order_id=str(position["order_id"]), fill_id=str(position["fill_id"]),
                            position_id=str(position["position_id"]), market_id=str(position["market_id"]),
                            event_id=str(position["event_id"]), token_id=str(position["token_id"]),
                            side="BUY", exchange_ts_ms=book.exchange_ts_ms,
                            receive_ts_ms=book.receive_ts_ms, book_snapshot_id=book.snapshot_id,
                            executable_liquidation_value=liquidation,
                            markouts={f"{horizon}s": per_share},
                            metadata={"model_family": STRATEGY, "horizon_seconds": 300,
                                      "full_visible_depth": True, "fill_conditioned": True},
                        ))
                        position.setdefault("markouts", []).append(horizon)
'''
replace_once(router_path, old_markout, new_markout)

# FINAL must enter the canonical single-writer ledger before the local position
# is declared settled. Deterministic IDs make a crash retry harmless.
replace_once(
    router_path,
    '''            self.state["counterfactual_realized_pnl"] = float(
                self.state.get("counterfactual_realized_pnl") or 0.0
            ) + pnl
            position["settled"] = True
            position["resolved_outcome"] = resolved
            won = winning_token == str(position["token_id"])
            self.emit_counterfactual(''',
    '''            won = winning_token == str(position["token_id"])
            canonical_metadata = {
                **self.canonical_position_metadata(position),
                "model_family": STRATEGY,
                "horizon_seconds": 300,
                "realized": True,
                "unwind_accounted": True,
                "cost_vector_complete": True,
                "settlement_outcome": resolved,
                "winning_token_id": winning_token,
                "won": won,
                "hold_to_settlement": True,
                "settlement_source": "POLYMARKET_GAMMA_PUBLIC",
                "settlement_timestamp_ms": current_ms,
                "settled_size": float(position["shares"]),
                "entry_notional": float(position["entry_cost"]),
                "entry_fees": float(position["entry_fee"]),
                "terminal_id": f"external-paper:{position['position_id']}:final",
                "pnl_decomposition": {
                    "trading_pnl": pnl, "spread_capture": 0.0,
                    "adverse_markout": 0.0, "inventory_pnl": 0.0,
                    "maker_rebates": 0.0, "liquidity_rewards": 0.0,
                    "own_reward_share_verified": False,
                },
            }
            spool_event(self.root, LedgerEvent(
                record_id=f"external-paper-final-{stable_id(position['position_id'], winning_token)}",
                event_type="FINAL", strategy=STRATEGY, model_sha=self.sha,
                model_version=MODEL_VERSION, order_id=str(position["order_id"]),
                position_id=str(position["position_id"]), market_id=str(position["market_id"]),
                event_id=str(position["event_id"]), token_id=str(position["token_id"]), side="BUY",
                final_pnl=pnl, realized_cashflow=pnl, fee=0.0, slippage=0.0,
                unwind_loss=0.0, capital_cost=0.0, latency_cost=0.0,
                capital_duration_ms=current_ms - int(position["opened_ms"]),
                metadata=canonical_metadata,
            ))
            self.emit_counterfactual(''',
)
replace_once(
    router_path,
    '''                },
            ))

    def publish(self, active_candidates: int, blocker: str = "") -> None:''',
    '''                },
            ))
            self.state["counterfactual_realized_pnl"] = float(
                self.state.get("counterfactual_realized_pnl") or 0.0
            ) + pnl
            position["settled"] = True
            position["resolved_outcome"] = resolved

    def publish(self, active_candidates: int, blocker: str = "") -> None:''',
)

# Publish both eligibility and sizing diagnostics.
replace_once(
    router_path,
    '''            "probe_candidates": int(self.state.get("probe_candidates") or 0),
            "probe_fills": int(self.state.get("probe_fills") or 0),
        })''',
    '''            "probe_candidates": int(self.state.get("probe_candidates") or 0),
            "probe_fills": int(self.state.get("probe_fills") or 0),
            "probe_diagnostics": self.state.get("last_probe_diagnostics") or {},
            "probe_size_diagnostics": self.state.get("last_probe_size_diagnostics") or {},
            "probe_capital_ceiling": (
                min(
                    float(self.state.get("starting_capital") or 0.0)
                    * float(self.probe_policy["max_capital_fraction"]),
                    float(self.probe_policy["max_notional_usd"]),
                    float(self.probe_policy["max_loss_usd"]),
                ) if self.probe_policy is not None else 0.0
            ),
        })''',
)
replace_once(
    router_path,
    '''        probe_rows: list[dict[str, Any]] = []
        if self.drain_path.exists():''',
    '''        probe_rows: list[dict[str, Any]] = []
        probe_diagnostics: dict[str, Any] = {
            "enabled": self.probe_policy is not None,
            "eligible": False,
            "reason": "PAPER_PROBE_UNASSESSED",
        }
        if self.drain_path.exists():''',
)
replace_once(
    router_path,
    '''            self.record_opportunity_set(status, books)
            robust_rows = robust_candidates(status, books, self.policy)''',
    '''            self.record_opportunity_set(status, books)
            probe_diagnostics = paper_probe_diagnostics(
                status, books, self.policy, self.probe_policy
            )
            self.state["last_probe_diagnostics"] = probe_diagnostics
            robust_rows = robust_candidates(status, books, self.policy)''',
)
replace_once(
    router_path,
    '''            elif rows:
                reason = (
                    "PAPER_PROBE_CANDIDATE_NOT_FILLED"
                    if probe_rows else "ROBUST_CANDIDATE_NOT_FILLED"
                )
            elif (status.get("fair") or {}).get("valid") and not entry_tte_allowed(''',
    '''            elif rows:
                reason = (
                    "PAPER_PROBE_CANDIDATE_NOT_FILLED"
                    if probe_rows else "ROBUST_CANDIDATE_NOT_FILLED"
                )
            elif (
                self.probe_policy is not None
                and probe_diagnostics.get("reason")
                not in {"PAPER_PROBE_CANDIDATE_READY", "PAPER_PROBE_UNASSESSED"}
            ):
                reason = str(probe_diagnostics["reason"])
            elif (status.get("fair") or {}).get("valid") and not entry_tte_allowed(''',
)
replace_once(
    router_path,
    '''                "probe_candidate_count": len(probe_rows),
                "candidate_count": len(rows),''',
    '''                "probe_candidate_count": len(probe_rows),
                "probe_diagnostics": probe_diagnostics,
                "probe_size_diagnostics": self.state.get("last_probe_size_diagnostics") or {},
                "candidate_count": len(rows),''',
)

# Update the checked-in safety documentation.
replace_once(
    "docs/v7_world_class/paper_exploration.md",
    "hard\nmaximum loss of 2 USD (also capped at 5 basis points of the engine sleeve).",
    "hard\nmaximum loss of 5 USD (also capped at 25 basis points of the engine sleeve).",
)

# Existing parser regression now tests the new independent hard ceiling.
replace_once(
    "tests/test_v7_paper_exploration_authority.py",
    'if mutation=="loss": value["exploration"]["maximum_probe_loss"]=2.01; value["exploration"]["probe_loss_cap"]=2.01',
    'if mutation=="loss": value["exploration"]["maximum_probe_loss"]=5.01; value["exploration"]["probe_loss_cap"]=5.01',
)

# Extend the existing router test without adding a new path to the repository
# classification manifest.
test_path = "tests/test_v7_external_fair_paper_router.py"
replace_once(
    test_path,
    'from v7_opportunity import OpportunityEnvelope  # noqa: E402\n',
    'from v7_opportunity import OpportunityEnvelope  # noqa: E402\nfrom v7_execution_ledger import LedgerEvent, load_events  # noqa: E402\nfrom v7_ledger_spool import drain_spool, spool_event  # noqa: E402\n',
)
life_test = r'''
    # The checked-in probe must be able to place one real CLOB minimum around
    # 50c, while remaining bounded by the independent USD 5 loss firewall.
    with tempfile.TemporaryDirectory() as directory:
        run_root = Path(directory)
        collector = router.PaperRouter(
            run_root, "9" * 40, ROOT / "config" / "v7_external_fair.json",
            "https://clob.invalid", "https://gamma.invalid",
        )
        probe_book = Book(
            "probe-token", ((0.49, 100.0),), ((0.50, 100.0),),
            0.01, 5.0, 1_000, 1_001, "probe-book",
        )
        probe_row = {
            "book": probe_book, "ask": 0.50, "fee_per_share": 0.0,
            "execution_risk": 0.0005, "paper_bootstrap_probe": True,
        }
        probe_size = collector.order_size(probe_row)
        assert probe_size >= probe_book.min_order_size
        sizing = collector.state["last_probe_size_diagnostics"]
        assert sizing["feasible"] is True
        assert sizing["reason"] == "PROBE_SIZE_READY"
        assert sizing["minimum_required_loss"] <= 5.0
        assert probe_size * 0.5005 <= 5.0 + 1e-9

    # A canonical PAPER fill must receive canonical MARKOUT and FINAL rows.
    # The receipt is the same coordinator receipt used at entry, and every
    # terminal record is accepted by the one ledger writer.
    with tempfile.TemporaryDirectory() as directory:
        run_root = Path(directory)
        sha = "8" * 40
        collector = router.PaperRouter(
            run_root, sha, ROOT / "config" / "v7_external_fair.json",
            "https://clob.invalid", "https://gamma.invalid",
        )
        receipt = {
            "schema": "polymarket_v7_global_opportunity_decision_v1",
            "owner": "V7_GLOBAL_PORTFOLIO_COORDINATOR",
            "engine_id": "CRYPTO_SETTLEMENT_ENGINE", "action": "TAKE",
            "selected_replay_key": "probe-replay", "new_risk_authorized": False,
            "paper_exploration_authorized": True,
            "paper_exploration_probe_authorized": True,
            "paper_only": True, "authenticated_execution": False,
            "real_order_submission": False, "real_capital_at_risk": False,
            "crypto_context": {"asset": "BTC", "horizon": "M5", "authority": "PAPER_EXPLORATION"},
        }
        canonical_metadata = {
            "coordinator_receipt": receipt, "paper_exploration": True,
            "paper_bootstrap_probe": True, "economic_authority": "PAPER_EXPLORATION",
            "counterfactual": False, "excluded_from_portfolio_equity": False,
            "research_evidence_only": False, "arrival_revalidated": True,
        }
        opened_ms = router.now_ms() - 301_000
        position = {
            "position_id": "probe-position", "counterfactual_id": "probe-candidate",
            "fill_id": "probe-fill", "order_id": "probe-order",
            "market_id": "probe-market", "event_id": "probe-event",
            "token_id": "yes", "outcome": "YES", "shares": 5.0,
            "entry_price": 0.50, "entry_fee": 0.0, "entry_cost": 2.50,
            "executable_value": 2.45, "opened_ms": opened_ms,
            "fee_schedule": {"rate": 0.0, "exponent": 1.0, "takerOnly": True},
            "markouts": [1, 10, 45, 60], "settled": False,
            "coordinator_receipt": receipt, "paper_exploration": True,
            "paper_bootstrap_probe": True, "canonical_metadata": canonical_metadata,
            "model_yes": 0.60, "market_yes": 0.50,
            "market_mid_source": "LIVE_COMPLEMENT_CONSISTENT_CLOB_BATCH",
        }
        collector.state["positions"] = {position["position_id"]: position}
        spool_event(run_root, LedgerEvent(
            record_id="probe-entry-fill", event_type="FILL", strategy=router.STRATEGY,
            model_sha=sha, model_version=router.MODEL_VERSION,
            order_id=position["order_id"], fill_id=position["fill_id"],
            position_id=position["position_id"], market_id=position["market_id"],
            event_id=position["event_id"], token_id=position["token_id"], side="BUY",
            exchange_ts_ms=opened_ms - 1, receive_ts_ms=opened_ms,
            fill_price=0.50, filled_size=5.0, complete=True,
            fee=0.0, fee_source="GAMMA_AUTHORITATIVE_FEE_SCHEDULE",
            metadata=canonical_metadata,
        ))
        live_book = Book(
            "yes", ((0.49, 100.0),), ((0.50, 100.0),), 0.01, 5.0,
            router.now_ms() - 2, router.now_ms() - 1, "settlement-book",
        )
        collector.fetch_book = lambda _token: live_book
        resolution = {
            "closed": True, "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["yes", "no"]', "outcomePrices": '["1", "0"]',
        }
        with mock.patch.object(router, "request_json", return_value=resolution):
            collector.observe_positions()
        result = drain_spool(run_root, model_sha=sha)
        assert result["quarantined"] == 0 and result["rejected"] == 0
        events = load_events(run_root / "ledger" / "execution.jsonl", expected_model_sha=sha)
        types = [event.event_type for event in events]
        assert types.count("FILL") == 1
        assert types.count("MARKOUT") == 1
        assert types.count("FINAL") == 1
        final = next(event for event in events if event.event_type == "FINAL")
        assert final.metadata["coordinator_receipt"]["selected_replay_key"] == "probe-replay"
        assert final.metadata["paper_bootstrap_probe"] is True
        assert position["settled"] is True

    # The durable VIRTUAL_FILL must restore the coordinator receipt and exact
    # order identity after a process restart.
    with tempfile.TemporaryDirectory() as directory:
        run_root = Path(directory)
        sha = "7" * 40
        collector = router.PaperRouter(
            run_root, sha, ROOT / "config" / "v7_external_fair.json",
            "https://clob.invalid", "https://gamma.invalid",
        )
        receipt = {
            "schema": "polymarket_v7_global_opportunity_decision_v1",
            "owner": "V7_GLOBAL_PORTFOLIO_COORDINATOR",
            "engine_id": "CRYPTO_SETTLEMENT_ENGINE", "action": "TAKE",
            "selected_replay_key": "restored-probe", "new_risk_authorized": False,
            "paper_exploration_authorized": True,
            "paper_exploration_probe_authorized": True,
            "paper_only": True, "authenticated_execution": False,
            "real_order_submission": False, "real_capital_at_risk": False,
            "crypto_context": {"asset": "BTC", "horizon": "M5", "authority": "PAPER_EXPLORATION"},
        }
        canonical_metadata = {
            "coordinator_receipt": receipt, "paper_exploration": True,
            "paper_bootstrap_probe": True, "economic_authority": "PAPER_EXPLORATION",
            "counterfactual": False, "excluded_from_portfolio_equity": False,
            "research_evidence_only": False,
        }
        collector.emit_counterfactual(
            "VIRTUAL_FILL", counterfactual_id="restore-candidate",
            order_id="restore-order", fill_id="restore-fill", position_id="restore-position",
            market_id="restore-market", event_id="restore-event", token_id="yes", side="BUY",
            receive_ts_ms=router.now_ms(), exchange_ts_ms=router.now_ms() - 1,
            fill_price=0.50, filled_size=5.0, fee=0.0,
            fee_schedule={"rate": 0.0, "exponent": 1.0, "takerOnly": True},
            metadata={
                "coordinator_receipt": receipt, "paper_exploration": True,
                "paper_bootstrap_probe": True, "economic_authority": "PAPER_EXPLORATION",
                "canonical_metadata": canonical_metadata,
            },
        )
        resumed = router.PaperRouter(
            run_root, sha, ROOT / "config" / "v7_external_fair.json",
            "https://clob.invalid", "https://gamma.invalid",
        )
        restored = resumed.state["positions"]["restore-position"]
        assert restored["order_id"] == "restore-order"
        assert restored["coordinator_receipt"]["selected_replay_key"] == "restored-probe"
        assert restored["canonical_metadata"]["paper_bootstrap_probe"] is True
        assert resumed.state["orders"] == 1 and resumed.state["fills"] == 1
        assert resumed.state["probe_fills"] == 1
'''
replace_once(
    test_path,
    '\n\nif __name__ == "__main__":\n    main()\n',
    life_test + '\n\nif __name__ == "__main__":\n    main()\n',
)

print("V7 probe lifecycle recovery patch applied")
