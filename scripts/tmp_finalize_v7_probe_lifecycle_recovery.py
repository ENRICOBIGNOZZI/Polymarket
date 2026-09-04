#!/usr/bin/env python3
from pathlib import Path

router = Path("scripts/v7_external_fair_paper_router.py")
text = router.read_text(encoding="utf-8")

old = '''        self.state["probe_fills"] = sum(
            int((row.get("metadata") or {}).get("paper_bootstrap_probe") is True)
            for row in fills
        )
'''
new = '''        self.state["probe_fills"] = sum(
            int((row.get("metadata") or {}).get("paper_bootstrap_probe") is True)
            for row in fills.values()
        )
'''
if old not in text:
    raise SystemExit("probe fill restore iteration anchor missing")
text = text.replace(old, new, 1)

# Historical VIRTUAL_FILL rows are research-only and have no coordinator
# receipt. Preserve their shadow settlement path; only receipt-backed PAPER
# positions are written to the canonical single-writer ledger.
old_markout = '''                    canonical_metadata = self.canonical_position_metadata(position)
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
'''
new_markout = '''                    canonical_metadata = (
                        self.canonical_position_metadata(position)
                        if position.get("paper_exploration") is True else None
                    )
                    for horizon in due:
                        if canonical_metadata is not None:
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
'''
if old_markout not in text:
    raise SystemExit("canonical markout compatibility anchor missing")
text = text.replace(old_markout, new_markout, 1)

old_final = '''            won = winning_token == str(position["token_id"])
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
'''
new_final = '''            won = winning_token == str(position["token_id"])
            if position.get("paper_exploration") is True:
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
'''
if old_final not in text:
    raise SystemExit("canonical final compatibility anchor missing")
text = text.replace(old_final, new_final, 1)

router.write_text(text, encoding="utf-8")
print("V7 probe lifecycle semantic finalizer applied")
