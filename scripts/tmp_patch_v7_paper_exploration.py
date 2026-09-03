from pathlib import Path
import json


def rep(path: str, old: str, new: str, count: int = 1) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"missing patch anchor: {path}: {old[:120]!r}")
    p.write_text(text.replace(old, new, count), encoding="utf-8")


# Explicit small, non-promotional exploration policy.
p = Path("config/v7_external_fair.json")
cfg = json.loads(p.read_text())
cfg["paper_exploration"] = {
    "enabled": True,
    "authority": "PAPER_EXPLORATION",
    "active_contract_families": ["BTC_USD_UPDOWN_5M"],
    "max_capital_fraction": 0.0025,
    "allow_immature_evidence": True,
    "promotion_credit": False,
    "require_arrival_book_revalidation": True,
    "require_verified_settlement": True,
    "real_money_authority": False,
}
p.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
p = Path("config/paper_v7.json")
paper = json.loads(p.read_text())
paper["v7"]["component_observation_budget_fractions"]["crypto_informed_taker"] = 0.0025
p.write_text(json.dumps(paper, indent=2, sort_keys=True) + "\n")

# Typed opportunity contract: separate exploration from mature PAPER authority.
path = "scripts/v7_opportunity.py"
rep(path,
    'or context.get("authority") not in {"SHADOW", "SHADOW_ZERO_AUTHORITY", "PAPER"}',
    'or context.get("authority") not in {"SHADOW", "SHADOW_ZERO_AUTHORITY", "PAPER_EXPLORATION", "PAPER"}')
rep(path,
'''        if action in NEW_RISK_ACTIONS and (
            latency.get("profile_valid") is not True
            or uncertainty.get("status") != "MATURE"
            or value.get("calibration_status") not in {"MATURE", "NOT_APPLICABLE"}
            or settlement.get("verified") is not True
            or float(capacity["executable_size"]) <= 0.0
        ):
            raise OpportunityError("new_risk_evidence_incomplete")
''',
'''        exploration = (
            engine_id == "CRYPTO_SETTLEMENT_ENGINE"
            and action in {"MAKE", "TAKE"}
            and isinstance(crypto_context, dict)
            and crypto_context.get("authority") == "PAPER_EXPLORATION"
        )
        if exploration:
            if (
                crypto_context.get("asset") != "BTC"
                or crypto_context.get("horizon") != "M5"
                or crypto_context.get("research_only") is not False
                or uncertainty.get("status") not in {"IMMATURE", "MATURE"}
                or value.get("calibration_status") not in {"IMMATURE", "MATURE", "NOT_APPLICABLE"}
                or settlement.get("verified") is not True
                or float(capacity["executable_size"]) <= 0.0
                or int(latency.get("arrival_ns") or -1) < 0
            ):
                raise OpportunityError("paper_exploration_evidence_incomplete")
        elif action in NEW_RISK_ACTIONS and (
            latency.get("profile_valid") is not True
            or uncertainty.get("status") != "MATURE"
            or value.get("calibration_status") not in {"MATURE", "NOT_APPLICABLE"}
            or settlement.get("verified") is not True
            or float(capacity["executable_size"]) <= 0.0
        ):
            raise OpportunityError("new_risk_evidence_incomplete")
''')
rep(path,
'        "new_risk_authorized": False,\n        "reasons": reasons or ["FAIL_CLOSED"],',
'        "new_risk_authorized": False,\n        "paper_exploration_authorized": False,\n        "reasons": reasons or ["FAIL_CLOSED"],')
rep(path,
'    new_risk_authorized: bool = False,\n) -> dict[str, Any]:',
'    new_risk_authorized: bool = False,\n    paper_exploration_authorized: bool = False,\n) -> dict[str, Any]:')
rep(path,
'            "new_risk_authorized": False,\n            "reasons": ["RISK_ACTION_PREEMPTS_ALPHA"],',
'            "new_risk_authorized": False,\n            "paper_exploration_authorized": False,\n            "reasons": ["RISK_ACTION_PREEMPTS_ALPHA"],')
rep(path,
'''    candidates = [row for row in live if row.action in NEW_RISK_ACTIONS]
    if not new_risk_authorized:
        return fail_closed_decision(now_ns=now_ns, reasons=["NEW_RISK_NOT_AUTHORIZED"])
    positive = [row for row in candidates if row.expected_wealth_change > 0.0]
''',
'''    candidates = [row for row in live if row.action in NEW_RISK_ACTIONS]
    exploration_candidates = [
        row for row in candidates
        if row.engine_id == "CRYPTO_SETTLEMENT_ENGINE"
        and isinstance(row.raw.get("crypto_context"), dict)
        and row.raw["crypto_context"].get("authority") == "PAPER_EXPLORATION"
    ]
    if not new_risk_authorized:
        if not paper_exploration_authorized:
            return fail_closed_decision(now_ns=now_ns, reasons=["NEW_RISK_NOT_AUTHORIZED"])
        positive = [row for row in exploration_candidates if row.expected_wealth_change > 0.0]
        if not positive:
            return fail_closed_decision(now_ns=now_ns, reasons=["NO_POSITIVE_PAPER_EXPLORATION_WEALTH_CHANGE"])
        selected = max(positive, key=lambda row: (row.expected_wealth_change, row.replay_key))
        return {
            "schema": "polymarket_v7_global_opportunity_decision_v1",
            "owner": "V7_GLOBAL_PORTFOLIO_COORDINATOR",
            "decision_timestamp_ns": int(now_ns),
            "action": selected.action,
            "engine_id": selected.engine_id,
            "crypto_context": selected.raw["crypto_context"],
            "selected_replay_key": selected.replay_key,
            "new_risk_authorized": False,
            "paper_exploration_authorized": True,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "real_capital_at_risk": False,
            "reasons": ["PAPER_EXPLORATION_MAX_CONSERVATIVE_EXPECTED_ACCOUNT_WEALTH_CHANGE"],
        }
    positive = [row for row in candidates if row.expected_wealth_change > 0.0]
''')
rep(path,
'        "new_risk_authorized": True,\n        "reasons": ["MAX_CONSERVATIVE_EXPECTED_ACCOUNT_WEALTH_CHANGE"],',
'        "new_risk_authorized": True,\n        "paper_exploration_authorized": False,\n        "reasons": ["MAX_CONSERVATIVE_EXPECTED_ACCOUNT_WEALTH_CHANGE"],')

# Single global coordinator grants only the distinct exploration authority.
path = "scripts/v7_global_portfolio_coordinator.py"
rep(path,
'decision = coordinate(envelopes, now_ns=current_ns, new_risk_authorized=False)',
'decision = coordinate(envelopes, now_ns=current_ns, new_risk_authorized=False, paper_exploration_authorized=True)')
rep(path,
'        "new_risk_policy": "CHECKED_IN_DISABLED_NO_RUNTIME_OVERRIDE",',
'        "new_risk_policy": "PAPER_EXPLORATION_ONLY_NO_REAL_MONEY",')
rep(path,
'''    atomic_json(root / "control" / "global_portfolio_coordinator.json", status)
    if files:
        append_jsonl(root / "opportunities" / "decisions.jsonl", decision)
''',
'''    atomic_json(root / "control" / "global_portfolio_coordinator.json", status)
    if (
        decision.get("paper_exploration_authorized") is True
        and decision.get("new_risk_authorized") is False
        and decision.get("action") in {"MAKE", "TAKE"}
        and isinstance(decision.get("selected_replay_key"), str)
        and decision.get("selected_replay_key")
    ):
        receipt_name = decision["selected_replay_key"].replace("/", "_") + ".json"
        atomic_json(root / "opportunities" / "receipts" / receipt_name, decision)
    if files:
        append_jsonl(root / "opportunities" / "decisions.jsonl", decision)
''')

# Single-writer ledger accepts exploration only with the coordinator receipt and real-money-disabled flags.
path = "scripts/v7_ledger_spool.py"
rep(path,
'''        and (
            event.event_type not in RISK_CREATING_EVENTS
            or receipt.get("new_risk_authorized") is True
        )
''',
'''        and (
            event.event_type not in RISK_CREATING_EVENTS
            or receipt.get("new_risk_authorized") is True
            or (
                engine_id == "CRYPTO_SETTLEMENT_ENGINE"
                and isinstance(event.metadata, dict)
                and event.metadata.get("paper_exploration") is True
                and event.metadata.get("economic_authority") == "PAPER_EXPLORATION"
                and receipt.get("paper_exploration_authorized") is True
                and receipt.get("new_risk_authorized") is False
                and receipt.get("paper_only") is True
                and receipt.get("authenticated_execution") is False
                and receipt.get("real_order_submission") is False
                and receipt.get("real_capital_at_risk") is False
                and isinstance(receipt.get("crypto_context"), dict)
                and receipt["crypto_context"].get("asset") == "BTC"
                and receipt["crypto_context"].get("horizon") == "M5"
                and receipt["crypto_context"].get("authority") == "PAPER_EXPLORATION"
            )
        )
''')

# External router: actionable candidate exists only after arrival-book revalidation.
path = "scripts/v7_external_fair_paper_router.py"
rep(path, 'from v7_execution_ledger import LedgerEvent\n', 'from v7_execution_ledger import LedgerEvent\nfrom v7_ledger_spool import spool_event\n')
rep(path, '    def emit_shadow_ingress(self, event: LedgerEvent) -> None:\n', '    def emit_shadow_ingress(self, event: LedgerEvent) -> str | None:\n')
rep(path, '        if evidence.event_type != "CANDIDATE":\n', '        if evidence.event_type != "CANDIDATE" or metadata.get("arrival_revalidated") is not True:\n')
rep(path, '            atomic_json(target, evidence.to_dict())\n            return\n\n        runtime = load(', '            atomic_json(target, evidence.to_dict())\n            return None\n\n        runtime = load(')
rep(path, '                "authority": self.crypto_context.authority,\n                "research_only": self.crypto_context.research_only,', '                "authority": "PAPER_EXPLORATION",\n                "research_only": False,')
rep(path, '            "action": "NOTHING",\n            "side": "NONE",', '            "action": "TAKE",\n            "side": "BUY",')
rep(path, '            "uncertainty": {"lower_bound": -1.0, "upper_bound": 1.0, "status": "MISSING"},\n            "calibration_status": "MISSING",', '            "uncertainty": {"lower_bound": fair_lower, "upper_bound": fair_upper, "status": "IMMATURE"},\n            "calibration_status": "IMMATURE",')
rep(path, '                "partial_fill_plan": "NO_NEW_RISK", "timeout_ms": 0,\n                "unwind_plan": "CANCEL_ONLY",', '                "partial_fill_plan": "CANCEL_REMAINDER", "timeout_ms": 1000,\n                "unwind_plan": "NONE",')
rep(path,
'''            "inventory_delta": 0.0,
            "portfolio_exposure_delta": 0.0,
            "settlement": {
                "definition": "counterfactual settlement binding is not promotion evidence",
                "source": "CRYPTO_SETTLEMENT_ENGINE_EXTERNAL_FAIR_COMPONENT", "verified": False,
            },''',
'''            "inventory_delta": quantity,
            "portfolio_exposure_delta": quantity * limit_price,
            "settlement": {
                "definition": "registry-verified BTC 5m Chainlink TWAP settlement binding",
                "source": "REGISTRY_VERIFIED_CHAINLINK_TWAP_60S",
                "verified": bool(metadata.get("contract_rules_hash")),
            },''')
rep(path,
'''            "reasons": [
                "ECONOMIC_EVIDENCE_MISSING", "SETTLEMENT_PROMOTION_UNVERIFIED",
                "NEW_RISK_DISABLED",
            ],''',
'''            "reasons": [
                "PAPER_EXPLORATION_ONLY", "ARRIVAL_BOOK_REVALIDATED",
                "IMMATURE_EVIDENCE_NO_PROMOTION_CREDIT",
            ],''')
rep(path, '        atomic_json(target, envelope)\n\n    def fetch_book', '''        atomic_json(target, envelope)
        return envelope["deterministic_replay_key"]

    def wait_for_exploration_receipt(self, replay_key: str, timeout_seconds: float = 1.0) -> dict[str, object] | None:
        path = self.root / "opportunities" / "receipts" / (replay_key.replace("/", "_") + ".json")
        deadline = time.monotonic() + max(0.1, timeout_seconds)
        while time.monotonic() < deadline:
            receipt = load(path)
            if (receipt.get("selected_replay_key") == replay_key
                    and receipt.get("paper_exploration_authorized") is True
                    and receipt.get("new_risk_authorized") is False
                    and receipt.get("paper_only") is True
                    and receipt.get("authenticated_execution") is False
                    and receipt.get("real_order_submission") is False
                    and receipt.get("real_capital_at_risk") is False):
                path.unlink(missing_ok=True)
                return receipt
            time.sleep(0.02)
        return None

    def fetch_book''')
rep(path, '            **common["metadata"], "robust_net_ev": robust_ev,\n            "arrival_snapshot_id": arrival_book.snapshot_id,', '            **common["metadata"], "robust_net_ev": robust_ev,\n            "arrival_revalidated": True,\n            "arrival_snapshot_id": arrival_book.snapshot_id,', 1)
anchor = '''        self.emit_shadow_ingress(LedgerEvent(
            event_type="ORDER_SUBMITTED", strategy=STRATEGY, model_sha=self.sha,
'''
insertion = '''        replay_key = self.emit_shadow_ingress(LedgerEvent(
            event_type="CANDIDATE", strategy=STRATEGY, model_sha=self.sha,
            model_version=MODEL_VERSION, candidate_id=counterfactual_id,
            order_id=order_id, position_id=position_id, market_id=market_id,
            event_id=str(market.get("event_id") or ""), token_id=row["token_id"], side="BUY",
            exchange_ts_ms=arrival_book.exchange_ts_ms, receive_ts_ms=arrival_book.receive_ts_ms,
            decision_ts_ms=arrival_decision_ms, book_snapshot_id=arrival_book.snapshot_id,
            limit_price=ask, intended_action="TAKE", intended_size=size,
            predicted_alpha=arrival["robust_ev"], expected_ev=robust_ev, fee=total_fee,
            fee_rate=float(schedule.get("rate") or 0.0), fee_source="GAMMA_AUTHORITATIVE_FEE_SCHEDULE",
            metadata=arrival_metadata,
        ))
        receipt = self.wait_for_exploration_receipt(str(replay_key or "")) if replay_key else None
        if receipt is None:
            self.emit_counterfactual("REJECTED", counterfactual_id=counterfactual_id,
                reason="PAPER_EXPLORATION_NOT_SELECTED", market_id=market_id,
                event_id=str(market.get("event_id") or ""), token_id=row["token_id"], side="BUY")
            self.last_attempt_reason = "PAPER_EXPLORATION_NOT_SELECTED"
            return False
        canonical_metadata = {
            **arrival_metadata, "coordinator_receipt": receipt, "paper_exploration": True,
            "economic_authority": "PAPER_EXPLORATION", "counterfactual": False,
            "excluded_from_portfolio_equity": False, "research_evidence_only": False,
        }
        spool_event(self.root, LedgerEvent(
            event_type="ORDER_SUBMITTED", strategy=STRATEGY, model_sha=self.sha,
            model_version=MODEL_VERSION, candidate_id=counterfactual_id,
            order_id=order_id, position_id=position_id, market_id=market_id,
            event_id=str(market.get("event_id") or ""), token_id=row["token_id"], side="BUY",
            exchange_ts_ms=arrival_book.exchange_ts_ms, receive_ts_ms=arrival_book.receive_ts_ms,
            decision_ts_ms=arrival_decision_ms, book_snapshot_id=arrival_book.snapshot_id,
            limit_price=ask, intended_action="TAKE", intended_size=size, order_state="SUBMITTED_SHADOW",
            predicted_alpha=arrival["robust_ev"], expected_ev=robust_ev, metadata=canonical_metadata,
        ))
        self.emit_shadow_ingress(LedgerEvent(
            event_type="ORDER_SUBMITTED", strategy=STRATEGY, model_sha=self.sha,
'''
rep(path, anchor, insertion)
fill_anchor = '''        self.emit_shadow_ingress(LedgerEvent(
            event_type="FILL", strategy=STRATEGY, model_sha=self.sha,
'''
fill_insert = '''        spool_event(self.root, LedgerEvent(
            event_type="FILL", strategy=STRATEGY, model_sha=self.sha,
            model_version=MODEL_VERSION, candidate_id=counterfactual_id, order_id=order_id,
            position_id=position_id, fill_id=fill_id, market_id=market_id,
            event_id=str(market.get("event_id") or ""), token_id=row["token_id"], side="BUY",
            exchange_ts_ms=arrival_book.exchange_ts_ms, receive_ts_ms=arrival_book.receive_ts_ms,
            fill_price=ask, filled_size=size, complete=True, fee=total_fee,
            fee_rate=float(schedule.get("rate") or 0.0), fee_source="GAMMA_AUTHORITATIVE_FEE_SCHEDULE",
            slippage=max(0.0, ask - float(common["ask"])) * size, metadata=canonical_metadata,
        ))
        self.emit_shadow_ingress(LedgerEvent(
            event_type="FILL", strategy=STRATEGY, model_sha=self.sha,
'''
rep(path, fill_anchor, fill_insert)
rep(path, '            "fee_schedule": schedule, "markouts": [], "settled": False,\n', '            "fee_schedule": schedule, "markouts": [], "settled": False,\n            "coordinator_receipt": receipt, "paper_exploration": True,\n')

# Regression tests across parser/coordinator/ledger + static router invariants.
Path("tests/test_v7_paper_exploration_authority.py").write_text('''from __future__ import annotations
import json, sys, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts")); sys.path.insert(0,str(ROOT/"tests"))
from v7_opportunity import OpportunityEnvelope, OpportunityError, coordinate
from v7_global_portfolio_coordinator import process_cut
from v7_execution_ledger import LedgerEvent
from v7_ledger_spool import spool_event, drain_spool
from test_v7_opportunity import envelope
SHA="1"*40

def exploration():
    v=envelope(authority="PAPER_EXPLORATION", key="explore")
    v["uncertainty"]["status"]="IMMATURE"; v["calibration_status"]="IMMATURE"
    v["latency"]["profile_valid"]=False; v["latency"]["arrival_ns"]=2_000_000
    return v

def test_exploration_is_btc_m5_only_and_not_mature_new_risk():
    v=exploration(); assert OpportunityEnvelope.parse(v).action=="TAKE"
    d=coordinate([v],now_ns=150,paper_exploration_authorized=True)
    assert d["action"]=="TAKE" and d["paper_exploration_authorized"] is True
    assert d["new_risk_authorized"] is False
    bad=exploration(); bad["crypto_context"]["asset"]="ETH"
    try: OpportunityEnvelope.parse(bad)
    except OpportunityError as e: assert str(e)=="paper_exploration_evidence_incomplete"
    else: raise AssertionError("ETH exploration accepted")

def test_coordinator_writes_separate_exploration_receipt():
    with tempfile.TemporaryDirectory() as td:
        root=Path(td); (root/"opportunities/inbox").mkdir(parents=True)
        (root/"opportunities/inbox/e.json").write_text(json.dumps(exploration()))
        d=process_cut(root,now_ns=150)["last_decision"]
        assert d["paper_exploration_authorized"] is True and d["new_risk_authorized"] is False
        assert list((root/"opportunities/receipts").glob("*.json"))

def test_ledger_accepts_only_explicit_paper_exploration_receipt():
    with tempfile.TemporaryDirectory() as td:
        root=Path(td)
        receipt={"schema":"polymarket_v7_global_opportunity_decision_v1","owner":"V7_GLOBAL_PORTFOLIO_COORDINATOR","engine_id":"CRYPTO_SETTLEMENT_ENGINE","action":"TAKE","selected_replay_key":"explore","new_risk_authorized":False,"paper_exploration_authorized":True,"paper_only":True,"authenticated_execution":False,"real_order_submission":False,"real_capital_at_risk":False,"crypto_context":{"asset":"BTC","horizon":"M5","authority":"PAPER_EXPLORATION"}}
        meta={"coordinator_receipt":receipt,"paper_exploration":True,"economic_authority":"PAPER_EXPLORATION"}
        ev=LedgerEvent(event_type="FILL",strategy="CRYPTO_INFORMED_TAKER",model_sha=SHA,order_id="o",fill_id="f",side="BUY",token_id="t",exchange_ts_ms=1000,receive_ts_ms=1100,fill_price=.5,filled_size=1,fee=0,fee_source="test:authoritative",metadata=meta)
        spool_event(root,ev); r=drain_spool(root,model_sha=SHA); assert r["appended"]==1 and r["quarantined"]==0
        receipt["real_order_submission"]=True
        ev2=LedgerEvent(event_type="FILL",strategy="CRYPTO_INFORMED_TAKER",model_sha=SHA,order_id="o2",fill_id="f2",side="BUY",token_id="t",exchange_ts_ms=1000,receive_ts_ms=1100,fill_price=.5,filled_size=1,fee=0,fee_source="test:authoritative",metadata=meta)
        spool_event(root,ev2); r=drain_spool(root,model_sha=SHA); assert r["quarantined"]==1

def test_router_requires_arrival_and_receipt_before_canonical_fill():
    text=(ROOT/"scripts/v7_external_fair_paper_router.py").read_text()
    assert "arrival_revalidated" in text
    assert "wait_for_exploration_receipt" in text
    assert "PAPER_EXPLORATION_NOT_SELECTED" in text
    assert text.index("replay_key = self.emit_shadow_ingress") < text.index("spool_event(self.root, LedgerEvent(\n            event_type=\"FILL\"")
''', encoding="utf-8")
