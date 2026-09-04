from __future__ import annotations
import json
from pathlib import Path
R=Path.cwd()

def edit(rel,a,b,n=1):
 p=R/rel;t=p.read_text();assert t.count(a)>=n,(rel,a[:70],t.count(a));p.write_text(t.replace(a,b,n))

# Economic maturity blocks broad exploitation, not the tiny controlled PAPER experiment.
inv='scripts/v7_external_fair_invariants.py'
edit(inv,
'''    if not model_mature and maker.get("external_fair_enabled_for_live_quotes") is not False:
        failures.append("IMMATURE_EXTERNAL_FAIR_MAY_NOT_REPRICE_MAKER")
    if not model_mature and taker_executes:
        failures.append("IMMATURE_TAKER_MAY_NOT_EXECUTE")
    if not taker_executes:''',
'''    if not model_mature and maker.get("external_fair_enabled_for_live_quotes") is not False:
        failures.append("IMMATURE_EXTERNAL_FAIR_MAY_NOT_REPRICE_MAKER")
    exploration = external.get("paper_exploration") if isinstance(
        external.get("paper_exploration"), dict) else {}
    bounded = bool(
        taker_executes and str(taker.get("authority") or "").upper() == "PAPER"
        and taker.get("execution_scope") == "PAPER_EXPLORATION_ONLY"
        and exploration.get("enabled") is True
        and exploration.get("authority") == "PAPER_EXPLORATION"
        and exploration.get("accounting_mode") == "CANONICAL_PAPER_ACCOUNT"
        and exploration.get("allow_immature_evidence") is True
        and exploration.get("require_arrival_book_revalidation") is True
        and exploration.get("require_verified_settlement") is True
        and exploration.get("promotion_credit") is False
        and exploration.get("real_money_authority") is False
        and 0.0 < float(exploration.get("max_capital_fraction") or 0.0) <= 0.0025)
    if not model_mature and taker_executes and not bounded:
        failures.append("IMMATURE_TAKER_MAY_ONLY_RUN_BOUNDED_PAPER_EXPLORATION")
    if not taker_executes:''')
edit(inv,
'''    elif str(taker.get("authority") or "").upper() != "PAPER":
        failures.append("EXECUTING_TAKER_REQUIRES_PAPER_AUTHORITY")
    if authority == "PAPER_CANCEL_ONLY_OWNER":''',
'''    elif str(taker.get("authority") or "").upper() != "PAPER":
        failures.append("EXECUTING_TAKER_REQUIRES_PAPER_AUTHORITY")
    elif not model_mature and taker.get("execution_scope") != "PAPER_EXPLORATION_ONLY":
        failures.append("IMMATURE_TAKER_EXECUTION_SCOPE_INVALID")
    if authority == "PAPER_CANCEL_ONLY_OWNER":''')

loop='scripts/paper_v7_execution_loop.sh'
edit(loop,
'''assert external.get("execution_authority") == "SHADOW_ZERO_AUTHORITY"
assert external.get("paper_only") is True
assert external.get("authenticated_execution") is False
assert external.get("real_order_submission") is False
assert external.get("taker",{}).get("authority") == "SHADOW"
assert external.get("taker",{}).get("enabled_for_execution") is False
assert external.get("taker",{}).get("counterfactual_enabled") is True''',
'''assert external.get("execution_authority") == "PAPER_EXECUTION_OWNER"
assert external.get("paper_only") is True
assert external.get("authenticated_execution") is False
assert external.get("real_order_submission") is False
assert external.get("taker",{}).get("authority") == "PAPER"
assert external.get("taker",{}).get("enabled_for_execution") is True
assert external.get("taker",{}).get("execution_scope") == "PAPER_EXPLORATION_ONLY"
assert external.get("taker",{}).get("counterfactual_enabled") is True
assert external.get("paper_exploration",{}).get("accounting_mode") == "CANONICAL_PAPER_ACCOUNT"''')

for rel in (loop,'ops/v7_runtime_supervisor.py'):
 p=R/rel;t=p.read_text()
 t=t.replace('FULL_FAIR_SHADOW_OPERATIONAL','FULL_FAIR_PAPER_OPERATIONAL')
 t=t.replace('value.get("execution_authority")=="OPPORTUNITY_PROPOSAL_ONLY"','value.get("execution_authority")=="PAPER_EXECUTION_OWNER"')
 t=t.replace('router.get("execution_authority") == "OPPORTUNITY_PROPOSAL_ONLY"','router.get("execution_authority") == "PAPER_EXECUTION_OWNER"')
 t=t.replace('value.get("order_submission_enabled") is False','value.get("order_submission_enabled") is True')
 t=t.replace('router.get("order_submission_enabled") is False','router.get("order_submission_enabled") is True')
 p.write_text(t)

p=R/'scripts/v7_rtds_external_fair_monitor.py';t=p.read_text()
t=t.replace('shadow_collector_active','paper_router_active')
t=t.replace('router.get("execution_authority") == "OPPORTUNITY_PROPOSAL_ONLY"','router.get("execution_authority") == "PAPER_EXECUTION_OWNER"')
t=t.replace('router.get("order_submission_enabled") is False','router.get("order_submission_enabled") is True')
t=t.replace('FULL_FAIR_SHADOW_OPERATIONAL','FULL_FAIR_PAPER_OPERATIONAL')
t=t.replace('"SHADOW_ZERO_AUTHORITY"\n            ),','"PAPER_EXECUTION_OWNER"\n            ),',1)
t=t.replace('LIVE_SHADOW_COMPARISON','LIVE_PAPER_COMPARISON')
t=t.replace('COUNTERFACTUAL_COLLECTOR_NOT_RUNNING','PAPER_ROUTER_NOT_RUNNING')
p.write_text(t)

p=R/'scripts/v7_profitability_audit.py';t=p.read_text()
t=t.replace('"execution_gate": "BLOCK_PAPER_EXECUTION_UNTIL_OOS_BENCHMARKS_PASS",',
'''"execution_gate": "ALLOW_BOUNDED_PAPER_EXPLORATION_ONLY",
        "mature_exploitation_gate": "BLOCK_UNTIL_OOS_BENCHMARKS_PASS",''',1)
t=t.replace('external["execution_gate"] != "BLOCK_PAPER_EXECUTION_UNTIL_OOS_BENCHMARKS_PASS"',
            'external["mature_exploitation_gate"] != "BLOCK_UNTIL_OOS_BENCHMARKS_PASS"',1)
p.write_text(t)

edit(loop,
'"economic_new_risk_ready":false,"economic_decision_state":"SAFE_ACTIONS_ONLY","authorized_alpha_actions":[],"safe_actions":["CANCEL","WITHDRAW","NOTHING"]}',
'"economic_new_risk_ready":false,"paper_exploration_ready":true,"paper_account_mode":"ACTIVE_SIMULATED","economic_decision_state":"BOUNDED_PAPER_EXPLORATION","authorized_alpha_actions":[],"authorized_paper_actions":["PAPER_EXPLORATION"],"safe_actions":["CANCEL","WITHDRAW","NOTHING"]}')
print('contract patch applied')