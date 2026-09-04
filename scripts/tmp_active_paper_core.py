from __future__ import annotations
import json
from pathlib import Path

R=Path.cwd()

def rw(rel, f):
 p=R/rel; t=p.read_text(); n=f(t); assert n!=t, rel; p.write_text(n)

def rep(t,a,b,n=1):
 assert t.count(a)>=n,(a[:80],t.count(a)); return t.replace(a,b,n)

# Activate one bounded, simulated BTC-M5 PAPER route. No authenticated or real submission.
p=R/'config/v7_external_fair.json'; c=json.loads(p.read_text())
c['execution_authority']='PAPER_EXECUTION_OWNER'; tk=c['taker']
tk.update(authority='PAPER',enabled_for_execution=True,
          execution_scope='PAPER_EXPLORATION_ONLY',counterfactual_enabled=True)
c['paper_exploration']['accounting_mode']='CANONICAL_PAPER_ACCOUNT'
e=c['gate_classes']['B_ECONOMIC_MATURITY']
e['may_block_bounded_paper_exploration']=False
e['may_block_mature_paper_exploitation']=True
p.write_text(json.dumps(c,indent=2)+'\n')

p=R/'scripts/v7_external_fair_paper_router.py'; t=p.read_text()
t=rep(t,'Collect settlement-aware External Fair counterfactuals without execution.',
      'Run settlement-aware BTC M5 orders in the canonical simulated PAPER account.')
t=rep(t,'Nothing from this component\ncan reach portfolio cash or authoritative PAPER PnL directly.',
      'Selected fills, positions, settlement cash and PnL enter the canonical PAPER account;\nthe counterfactual tape remains a separate research mirror.')
t=rep(t,'self.config.get("execution_authority") != "SHADOW_ZERO_AUTHORITY"',
      'self.config.get("execution_authority") != "PAPER_EXECUTION_OWNER"')
t=rep(t,'external_fair_shadow_contract_invalid','external_fair_paper_contract_invalid')
t=rep(t,'self.policy.get("enabled_for_execution") is not False',
      'self.policy.get("enabled_for_execution") is not True')
t=rep(t,'self.policy.get("authority") != "SHADOW"',
      'self.policy.get("authority") != "PAPER"')
t=rep(t,'or self.policy.get("counterfactual_enabled") is not True):',
      'or self.policy.get("execution_scope") != "PAPER_EXPLORATION_ONLY"\n                or self.policy.get("counterfactual_enabled") is not True):',1)
t=rep(t,'external_fair_taker_not_shadow_authorized',
      'external_fair_taker_not_bounded_paper_authorized')
t=rep(t,'            "peak_equity": starting_capital, "killed": False,',
      '            "peak_equity": starting_capital,\n            "counterfactual_peak_equity": starting_capital, "killed": False,')
t=rep(t,'                "markouts": sorted(markout_horizons.get(fill_id, set())),\n                "settled": False,',
      '                "markouts": sorted(markout_horizons.get(fill_id, set())),\n                "paper_accounted": False,\n                "settled": False,')
old='''        canonical_metadata = {
            **arrival_metadata, "coordinator_receipt": receipt, "paper_exploration": True,
            "paper_bootstrap_probe": is_probe,
            "economic_authority": "PAPER_EXPLORATION", "counterfactual": False,
            "excluded_from_portfolio_equity": False, "research_evidence_only": False,
        }'''
new='''        canonical_metadata = {
            **arrival_metadata, "coordinator_receipt": receipt, "paper_exploration": True,
            "paper_bootstrap_probe": is_probe,
            "economic_authority": "PAPER_EXPLORATION",
            "execution_authority": "PAPER_EXECUTION_OWNER",
            "authority": "PAPER_EXECUTION_OWNER", "execution_mode": "PAPER_SIMULATED",
            "paper_account": True, "paper_tif": "FAK", "counterfactual": False,
            "excluded_from_portfolio_equity": False, "research_evidence_only": False,
            "ledger_writer_authority": False,
        }'''
t=rep(t,old,new)
t=rep(t,'order_state="SUBMITTED_SHADOW"','order_state="SUBMITTED_PAPER_FAK"')
t=rep(t,'        self.state["counterfactual_fills"] = int(self.state.get("counterfactual_fills") or 0) + 1\n        if is_probe:',
'''        self.state["orders"] = int(self.state.get("orders") or 0) + 1
        self.state["fills"] = int(self.state.get("fills") or 0) + 1
        self.state["cash"] = float(self.state.get("cash") or 0.0) - cost - total_fee
        self.state["counterfactual_fills"] = int(self.state.get("counterfactual_fills") or 0) + 1
        if is_probe:''')
t=rep(t,'            "fee_schedule": schedule, "markouts": [], "settled": False,',
      '            "fee_schedule": schedule, "markouts": [],\n            "paper_accounted": True, "settled": False,')
t=rep(t,'self.last_attempt_reason = "VIRTUAL_FILL"','self.last_attempt_reason = "PAPER_FILL"')

# Replace the markout lifecycle block between stable anchors.
s=t.index('                        self.emit_shadow_ingress(LedgerEvent(\n                            event_type="MARKOUT"')
e=t.index('                        position.setdefault("markouts", []).append(horizon)',s)+len('                        position.setdefault("markouts", []).append(horizon)')
mark='''                        paper_metadata = {
                            "model_family": STRATEGY, "horizon_seconds": 300,
                            "full_visible_depth": True, "fill_conditioned": True,
                            "coordinator_receipt": position.get("coordinator_receipt"),
                            "paper_exploration": True,
                            "paper_bootstrap_probe": position.get("paper_bootstrap_probe") is True,
                            "economic_authority": "PAPER_EXPLORATION",
                            "execution_authority": "PAPER_EXECUTION_OWNER",
                            "execution_mode": "PAPER_SIMULATED", "paper_account": True,
                            "counterfactual": False, "excluded_from_portfolio_equity": False,
                            "research_evidence_only": False, "ledger_writer_authority": False,
                        }
                        if position.get("paper_accounted") is True:
                            spool_event(self.root, LedgerEvent(
                                event_type="MARKOUT", strategy=STRATEGY, model_sha=self.sha,
                                model_version=MODEL_VERSION,
                                record_id=stable_id("PAPER_MARKOUT", self.sha, position["fill_id"], horizon),
                                order_id=str(position["order_id"]), fill_id=str(position["fill_id"]),
                                position_id=str(position["position_id"]), market_id=str(position["market_id"]),
                                event_id=str(position["event_id"]), token_id=str(position["token_id"]),
                                side="BUY", exchange_ts_ms=book.exchange_ts_ms,
                                receive_ts_ms=book.receive_ts_ms, book_snapshot_id=book.snapshot_id,
                                executable_liquidation_value=liquidation,
                                markouts={f"{horizon}s": per_share}, metadata=paper_metadata,
                            ))
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
                        position.setdefault("markouts", []).append(horizon)'''
t=t[:s]+mark+t[e:]

# Replace settlement section up to publish().
s=t.index('            self.state["counterfactual_realized_pnl"] = float(',t.index('payout = float(position["shares"])'))
e=t.index('\n\n    def publish(',s)
settle='''            self.state["counterfactual_realized_pnl"] = float(
                self.state.get("counterfactual_realized_pnl") or 0.0) + pnl
            paper_accounted = position.get("paper_accounted") is True
            if paper_accounted:
                self.state["cash"] = float(self.state.get("cash") or 0.0) + payout
                self.state["realized_pnl"] = float(self.state.get("realized_pnl") or 0.0) + pnl
            position.update(settled=True, resolved_outcome=resolved,
                            settlement_payout=payout, final_pnl=pnl)
            won = winning_token == str(position["token_id"])
            self.emit_counterfactual(
                "VIRTUAL_FINAL", strategy=STRATEGY, model_version=MODEL_VERSION,
                counterfactual_id=str(position["counterfactual_id"]),
                position_id=str(position["position_id"]), fill_id=str(position["fill_id"]),
                market_id=str(position["market_id"]), event_id=str(position["event_id"]),
                token_id=str(position["token_id"]), side="BUY", counterfactual_pnl=pnl,
                virtual_cashflow=payout, capital_duration_ms=current_ms-int(position["opened_ms"]),
                metadata={"settlement_outcome":resolved,"winning_token_id":winning_token,
                          "won":won,"hold_to_settlement":True,"counterfactual":True,
                          "model_yes":position.get("model_yes"),"market_yes":position.get("market_yes"),
                          "model_family":STRATEGY,"horizon_seconds":300})
            terminal = {
                "model_family":STRATEGY,"horizon_seconds":300,"settlement_outcome":resolved,
                "winning_token_id":winning_token,"won":won,"hold_to_settlement":True,
                "coordinator_receipt":position.get("coordinator_receipt"),"paper_exploration":True,
                "paper_bootstrap_probe":position.get("paper_bootstrap_probe") is True,
                "economic_authority":"PAPER_EXPLORATION","execution_authority":"PAPER_EXECUTION_OWNER",
                "execution_mode":"PAPER_SIMULATED","paper_account":True,"counterfactual":False,
                "excluded_from_portfolio_equity":False,"research_evidence_only":False,
                "ledger_writer_authority":False,"realized":True,"unwind_accounted":True,
                "cost_vector_complete":True,"rebate_authority":"CONSERVATIVE_ZERO",
                "terminal_id":f"external-paper:{position['position_id']}:final",
                "pnl_decomposition":{"trading_pnl":pnl,"spread_capture":0.0,"adverse_markout":0.0,
                                     "inventory_pnl":0.0,"maker_rebates":0.0,
                                     "liquidity_rewards":0.0,"own_reward_share_verified":False}}
            if paper_accounted:
                spool_event(self.root, LedgerEvent(
                    event_type="FINAL",strategy=STRATEGY,model_sha=self.sha,model_version=MODEL_VERSION,
                    record_id=stable_id("PAPER_FINAL",self.sha,position["position_id"]),
                    order_id=str(position["order_id"]),fill_id=str(position["fill_id"]),
                    position_id=str(position["position_id"]),market_id=str(position["market_id"]),
                    event_id=str(position["event_id"]),token_id=str(position["token_id"]),side="BUY",
                    final_pnl=pnl,realized_cashflow=payout,fee=0.0,slippage=0.0,unwind_loss=0.0,
                    capital_cost=0.0,latency_cost=0.0,
                    capital_duration_ms=current_ms-int(position["opened_ms"]),metadata=terminal))
            self.emit_shadow_ingress(LedgerEvent(
                event_type="FINAL",strategy=STRATEGY,model_sha=self.sha,model_version=MODEL_VERSION,
                order_id=str(position["order_id"]),fill_id=str(position["fill_id"]),
                position_id=str(position["position_id"]),market_id=str(position["market_id"]),
                event_id=str(position["event_id"]),token_id=str(position["token_id"]),side="BUY",
                final_pnl=pnl,realized_cashflow=payout,fee=0.0,slippage=0.0,unwind_loss=0.0,
                capital_cost=0.0,latency_cost=0.0,
                capital_duration_ms=current_ms-int(position["opened_ms"]),metadata=terminal))'''
t=t[:s]+settle+t[e:]

# Replace publish accounting head.
s=t.index('        positions = self.state.get("positions")',t.index('    def publish('))
e=t.index('        atomic_json(self.state_path, self.state)',s)
head='''        positions = self.state.get("positions") if isinstance(self.state.get("positions"), dict) else {}
        paper_positions = [p for p in positions.values()
                           if isinstance(p, dict) and p.get("paper_accounted") is True]
        open_positions = sum(1 for p in paper_positions if not p.get("settled"))
        counterfactual_open_positions = sum(1 for p in positions.values()
            if isinstance(p,dict) and not p.get("settled"))
        starting_capital = float(self.state.get("starting_capital") or 0.0)
        cash = float(self.state.get("cash") or 0.0)
        equity = cash + sum(float(p.get("executable_value") or 0.0)
                            for p in paper_positions if not p.get("settled"))
        virtual_equity = starting_capital + float(self.state.get("counterfactual_realized_pnl") or 0.0)
        virtual_equity += sum(float(p.get("executable_value") or 0.0)
            -float(p.get("entry_cost") or 0.0)-float(p.get("entry_fee") or 0.0)
            for p in positions.values() if isinstance(p,dict) and not p.get("settled"))
        peak=max(starting_capital,float(self.state.get("peak_equity") or starting_capital),equity)
        cpeak=max(starting_capital,float(self.state.get("counterfactual_peak_equity") or starting_capital),virtual_equity)
        drawdown=max(0.0,1.0-equity/peak) if peak>0 else 1.0
        cdrawdown=max(0.0,1.0-virtual_equity/cpeak) if cpeak>0 else 1.0
        killed=bool(self.state.get("killed")) or drawdown>=0.15 or (self.root/"control"/"KILL").exists()
        drain_requested=self.drain_path.exists()
        if drain_requested: blocker="CUTOVER_DRAIN"
        self.state.update(peak_equity=peak,counterfactual_peak_equity=cpeak,killed=killed)
'''
t=t[:s]+head+t[e:]
for a,b in (
 ('"execution_mode": "SHADOW_COUNTERFACTUAL"','"execution_mode": "PAPER_SIMULATED"'),
 ('"execution_authority": "OPPORTUNITY_PROPOSAL_ONLY"','"execution_authority": "PAPER_EXECUTION_OWNER"'),
 ('"MATURE_SHADOW_FRACTIONAL_KELLY"','"MATURE_PAPER_FRACTIONAL_KELLY"'),
 ('"IMMATURE_SHADOW_FIXED_NOTIONAL"','"IMMATURE_BOUNDED_PAPER_EXPLORATION"'),
 ('"counterfactual_open_positions": open_positions','"counterfactual_open_positions": counterfactual_open_positions'),
 ('"counterfactual_peak_equity": peak, "counterfactual_drawdown": drawdown',
  '"counterfactual_peak_equity": cpeak, "counterfactual_drawdown": cdrawdown'),
 ('"open_positions": 0, "realized_pnl": 0.0,\n            "cash": starting_capital, "equity": starting_capital',
  '"open_positions": open_positions, "realized_pnl": float(self.state.get("realized_pnl") or 0.0),\n            "cash": cash, "equity": equity'),
 ('"peak_equity": starting_capital, "drawdown": 0.0','"peak_equity": peak, "drawdown": drawdown'),
 ('"order_submission_enabled": False','"order_submission_enabled": not killed and not drain_requested and not blocker'),
 ('"drain_requested": drain_requested, "drain_complete": drain_requested',
  '"drain_requested": drain_requested, "drain_complete": drain_requested and open_positions == 0'),
 ('"TAKE": 0','"TAKE": int(self.state.get("orders") or 0)'),
 ('reason = "VIRTUAL_FILL"','reason = "PAPER_FILL"')):
 t=rep(t,a,b,1)
p.write_text(t)
print('core patch applied')