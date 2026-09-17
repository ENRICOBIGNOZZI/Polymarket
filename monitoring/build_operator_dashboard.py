#!/usr/bin/env python3
"""Deterministic Grafana operator dashboard builder; native Grafana panels only."""
from __future__ import annotations
import copy
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
DS = {"type": "prometheus", "uid": "prometheus-v7"}
FILTER = 'job="polymarket-v7",instance="$instance"'
NO_VALUE = "UNAVAILABLE"


def scoped(expr: str) -> str:
    def add(match):
        name, labels = match.group(1), match.group(2)
        return name + "{" + (labels[1:-1] + "," if labels else "") + FILTER + "}"
    return re.sub(r"\b(polymarket_[a-zA-Z0-9_]+|ALERTS|up)(\{[^}]*\})?", add, expr)


def query(expr: str, source: str | None = None, *, history: bool = False) -> str:
    if expr.startswith("ALERTS"):
        return scoped(expr)
    value = "(" + scoped(expr) + ")"
    if history:
        value += " * on(job,instance) group_left(run_id) " + scoped("polymarket_v7_runtime_identity_info")
    value += " and on(job,instance) (" + scoped("polymarket_v7_exporter_snapshot_usable") + " == 1)"
    value += " and on(job,instance) (" + scoped("up") + " == 1)"
    inferred = set()
    if any(m in expr for m in ("polymarket_runtime_equity", "polymarket_runtime_pnl", "polymarket_runtime_drawdown", "polymarket_runtime_killed")):
        inferred.add("portfolio")
    if "polymarket_runtime_realized" in expr or "polymarket_v7_canonical_" in expr:
        inferred.add("economics")
    if "polymarket_v7_lead_lag_collector_" in expr:
        inferred.add("lead_lag_collector")
    elif "polymarket_v7_lead_lag_" in expr:
        inferred.add("lead_lag")
    if "polymarket_execution_" in expr:
        value += " and on(job,instance) (" + scoped("polymarket_v7_ledger_current_runtime") + " == 1)"
    for extra_source in sorted(inferred - {source}):
        value += " and on(job,instance) (" + scoped('polymarket_v7_source_fresh{source="' + extra_source + '"}') + " == 1)"
    if source:
        value += " and on(job,instance) (" + scoped('polymarket_v7_source_fresh{source="' + source + '"}') + " == 1)"
    return value


def defaults(unit="short", decimals=0):
    return {"unit": unit, "decimals": decimals, "noValue": NO_VALUE,
            "color": {"mode": "palette-classic"}, "mappings": [
                {"type": "special", "options": {"match": "null", "result": {"text": NO_VALUE, "color": "gray"}}},
                {"type": "special", "options": {"match": "nan", "result": {"text": NO_VALUE, "color": "gray"}}},
            ]}


def target(expr, legend, ref, source=None, history=False):
    return {"refId": ref, "datasource": DS, "expr": query(expr, source, history=history),
            "legendFormat": legend, "instant": not history, "range": history, "editorMode": "code"}


def stat(pid, title, expr, x, y, w=4, unit="short", decimals=0, source=None, mapping=None, description="", raw=False):
    short_titles = {101:"Attention",102:"Runtime",103:"Accounting",104:"Order mode",105:"Data age",106:"Exporter",111:"Verified PnL",112:"Ledger PnL",113:"Equity change",114:"Forward PnL",115:"PAPER equity",116:"Drawdown",121:"Candidates",122:"Receipts",123:"Entries",124:"Open positions",125:"Settled",126:"Rejected",129:"Last event",141:"Submitted",142:"Completed",143:"Completion",145:"Terminal events",146:"Capital hours",162:"Single writer",163:"Exact SHA",164:"Kill switch",165:"Restarts",166:"Free disk",169:"Runtime uptime",170:"Risk authority",171:"Engine count",183:"Scan complete",184:"Pages",191:"Latency data"}
    full_title = title
    title = short_titles.get(pid, title)
    if title != full_title:
        description = full_title + ". " + description
    d = defaults("prefix:$" if unit == "currencyUSD" else unit, decimals)
    if mapping:
        d["color"] = {"mode": "thresholds"}
        d["mappings"].append({"type": "value", "options": {str(k): {"text": text, "color": color} for k, (text,color) in mapping.items()}})
    p = {"id": pid, "type": "stat", "title": title, "description": description, "datasource": DS,
         "gridPos": {"x": x, "y": y, "w": w, "h": 4}, "fieldConfig": {"defaults": d, "overrides": []},
         "targets": [target(expr, title, "A", source)],
         "options": {"reduceOptions": {"calcs": ["last"], "fields": "", "values": False}, "orientation": "auto", "textMode": "auto", "colorMode": "value" if mapping else "none", "graphMode": "none", "justifyMode": "auto"}}
    if raw:
        p["targets"][0]["expr"] = scoped(expr)
    return p


def chart(pid, title, series, x, y, w=12, unit="short", decimals=0, source=None, description=""):
    d = defaults(unit, decimals)
    d["custom"] = {"drawStyle": "line", "lineInterpolation": "stepAfter", "lineWidth": 2, "fillOpacity": 5,
                   "spanNulls": False, "insertNulls": False, "showPoints": "never", "axisCenteredZero": False, "axisLabel": "", "axisPlacement": "auto", "scaleDistribution": {"type": "linear"}}
    return {"id": pid, "type": "timeseries", "title": title, "description": description, "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": 8}, "fieldConfig": {"defaults": d, "overrides": []},
            "targets": [target(expr, legend, chr(65+i), source, True) for i,(expr,legend) in enumerate(series)],
            "options": {"tooltip": {"mode": "multi", "sort": "desc"}, "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom", "calcs": []}}}


def table(pid, title, expr, x, y, w=12, source=None, description="", columns=None):
    return {"id": pid, "type": "table", "title": title, "description": description, "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": 6},
            "targets": [{**target(expr, "", "A", source), "format": "table"}],
            "fieldConfig": {"defaults": {**defaults("none"), "custom": {"align": "auto", "cellOptions": {"type": "auto"}, "filterable": True}}, "overrides": [{"matcher": {"id": "byRegexp", "options": "^(?!Value$).*"}, "properties": [{"id": "unit", "value": "string"}]}]},
            "transformations": [{"id": "organize", "options": {"excludeByName": {"Time": True, "__name__": True, "job": True, "instance": True, **({"Value": True} if columns else {})}, "renameByName": columns or {}}}],
            "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}}}


def row(pid, title, y, children=None):
    return {"id": pid, "type": "row", "title": title, "collapsed": children is not None,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": children or []}


def build():
    p = []
    p.append({"id":100,"type":"text","title":"Data rules","gridPos":{"x":0,"y":0,"w":24,"h":2},"options":{"mode":"markdown","content":"**PAPER ONLY.** Missing data ≠ zero."}})
    p += [
        stat(101,"Operator attention","polymarket_v7_operator_attention_required",0,2,mapping={0:("CLEAR","green"),1:("ATTENTION","red")}),
        stat(102,"PAPER runtime","polymarket_v7_execution_alive",4,2,source="runtime",mapping={0:("STOPPED","red"),1:("RUNNING","green")}),
        stat(103,"Accounting verification","polymarket_v7_accounting_verified",8,2,mapping={0:("NOT VERIFIED","red"),1:("RECONCILED","green")}),
        stat(104,"No real orders","polymarket_v7_paper_only_contract_ok * polymarket_v7_authenticated_execution_disabled",12,2,source="runtime",mapping={0:("UNSAFE","red"),1:("PAPER ONLY","green")}),
        stat(105,"Snapshot age","time() - polymarket_v7_exporter_snapshot_generated_unixtime",16,2,unit="s",decimals=1,raw=True,description="Age of the exporter cache. Values disappear after 45 seconds, even when Prometheus can still scrape the exporter."),
        stat(106,"Exporter connection","up",20,2,raw=True,mapping={0:("OFFLINE","red"),1:("REACHABLE","green")},description="Prometheus scrape reachability only. Inspect snapshot age and source freshness separately."),
    ]
    p[-2]["fieldConfig"]["defaults"].update({"color":{"mode":"thresholds"},"thresholds":{"mode":"absolute","steps":[{"color":"green","value":None},{"color":"yellow","value":20},{"color":"red","value":45}]}})
    p[-2]["options"]["colorMode"]="value"
    p += [table(107,"What needs attention now","polymarket_v7_operator_reason",0,6,description="Includes source validity, accounting discrepancies and disk pressure. A running process is not proof of correct accounting.",columns={"reason":"Reason"}),
          table(108,"Active deployment","polymarket_v7_runtime_location_info",12,6,source="runtime",columns={"server":"Host","sha":"Runtime SHA","run_id":"Run"})]
    p.append(row(110,"PAPER economics — evidence is not verified account performance",12))
    p += [
        stat(111,"Verified net PnL","polymarket_v7_verified_net_pnl_usd",0,13,unit="currencyUSD",decimals=2,description="Unavailable until current ledger, economics and portfolio reconcile. No artificial zero."),
        stat(112,"Ledger net PnL · evidence","polymarket_runtime_realized_pnl_usd",4,13,unit="currencyUSD",decimals=2,source="economics",description="canonical_economics.net_pnl. Evidence-collection accounting only; NOT an account-level verified return."),
        stat(113,"Guard equity change","polymarket_runtime_pnl_usd",8,13,unit="currencyUSD",decimals=2,source="portfolio",description="Reported guard equity less starting capital. May exclude forward-test components; not interchangeable with ledger net PnL."),
        stat(114,"Forward test · realized","polymarket_v7_lead_lag_realized_pnl_usd",12,13,unit="currencyUSD",decimals=2,source="lead_lag",description="LEAD_LAG_TAKER_V1 PAPER forward test. Included in canonical ledger; do not add it again to ledger PnL."),
        stat(115,"Reported PAPER equity","polymarket_runtime_equity_usd",16,13,unit="currencyUSD",decimals=2,source="portfolio"),
        stat(116,"Reported drawdown","polymarket_runtime_drawdown_ratio",20,13,unit="percentunit",decimals=3,source="portfolio"),
        chart(117,"PnL sources · keep the differences visible",[("polymarket_runtime_realized_pnl_usd","ledger net"),("polymarket_runtime_pnl_usd","guard equity change"),("polymarket_v7_lead_lag_realized_pnl_usd","forward test realized")],0,17,unit="currencyUSD",decimals=2,description="These measures are not additive. See accounting verification above. Series are split by runtime identity."),
        chart(118,"Reported equity · separate scale",[("polymarket_runtime_equity_usd","guard equity")],12,17,unit="currencyUSD",decimals=2,source="portfolio"),
    ]
    p.append(row(120,"LEAD_LAG_TAKER_V1 · PAPER forward test · no automatic promotion",25))
    for i,(title,field) in enumerate((("Candidates","candidate_count"),("Coordinator receipts","receipt_count"),("PAPER entries","entries"),("Open positions","open_positions"),("Settled markets","settled"),("Arrival rejections","arrival_rejections"))):
        p.append(stat(121+i,title,"polymarket_v7_lead_lag_"+field,i*4,26,source="lead_lag"))
    p += [table(127,"Forward-test state","polymarket_v7_lead_lag_status_info",0,30,w=8,source="lead_lag",columns={"state":"State","mode":"Mode"}),
          table(128,"Last forward-test event","polymarket_v7_lead_lag_last_event_info",8,30,w=8,source="lead_lag",columns={"event":"Event","market":"Market","outcome":"Outcome"}),
          stat(129,"Time since last event","polymarket_v7_lead_lag_last_event_age_seconds",16,30,w=8,unit="s",decimals=1,source="lead_lag",description="Time since the event, not the heartbeat. No recent fills does not imply an outage.")]
    p += [chart(130,"Forward-test funnel · cumulative this run",[("polymarket_v7_lead_lag_candidate_count","candidates"),("polymarket_v7_lead_lag_entries","entries"),("polymarket_v7_lead_lag_settled","settled"),("polymarket_v7_lead_lag_arrival_rejections","arrival rejected")],0,36,source="lead_lag"),
          table(131,"Why checks are skipped · not independent signals","sort_desc(polymarket_v7_lead_lag_skip_checks_total)",12,36,source="lead_lag",description="Cumulative repeated loop checks, NOT counts of unique trading opportunities or statistical observations.",columns=None)]
    p[-1]["gridPos"]["h"]=8
    p.append(row(140,"Canonical ledger · economic units are not fill-event ratios",44))
    p += [stat(141,"Submitted economic units","polymarket_v7_canonical_submitted_units",0,45,source="economics"),
          stat(142,"Completed economic units","polymarket_v7_canonical_complete_units",4,45,source="economics"),
          stat(143,"Economic Completion Rate","(polymarket_v7_canonical_complete_units / clamp_min(polymarket_v7_canonical_submitted_units, 1)) and on(job,instance) (polymarket_v7_canonical_submitted_units > 0)",8,45,unit="percentunit",decimals=2,source="economics",description="Undefined with zero submissions. Completed economic units / submitted economic units; not order fill probability."),
          stat(144,"Fill events","polymarket_execution_fills",12,45),
          stat(145,"Terminal events","polymarket_v7_ledger_terminal_events",16,45),
          stat(146,"Capital hours","polymarket_execution_capital_hours",20,45,decimals=3)]
    p += [chart(147,"Canonical events · cumulative this run",[("polymarket_execution_orders_submitted","orders"),("polymarket_execution_fills","fills"),("polymarket_execution_complete_fills","complete leg fills"),("polymarket_execution_partial_fills","partial leg fills"),("polymarket_execution_unwinds","unwinds")],0,49),
          chart(148,"Executable markout · measured observations only",[("polymarket_execution_mean_markout and on(job,instance,horizon) (polymarket_execution_markout_observations > 0)","{{horizon}}")],12,49,unit="currencyUSD",decimals=4,description="No observations = unavailable, not a flat zero performance line.")]
    p.append(row(150,"Health, sources and alerts",57))
    p += [chart(151,"Source ages · seconds",[("polymarket_v7_source_age_seconds","{{source}}")],0,58,unit="s",decimals=1),
          table(152,"Active alerts · firing and pending",'ALERTS{alertstate=~"firing|pending"}',12,58,description="No rows means no matching alerts at the selected time. Inspect exporter freshness above before treating an empty table as healthy.",columns={"alertname":"Alert","severity":"Severity","alertstate":"State"})]
    p[-1]["gridPos"]["h"]=8
    by_id = {panel["id"]: panel for panel in p}
    by_id[129]["gridPos"]["h"] = 6
    by_id[128]["fieldConfig"]["overrides"].extend([
        {"matcher": {"id": "byName", "options": name}, "properties": [{"id": "custom.width", "value": width}]}
        for name, width in (("Event", 80), ("Market", 120), ("Outcome", 90))
    ])
    by_id[127]["fieldConfig"]["overrides"].append({"matcher": {"id": "byName", "options": "Mode"}, "properties": [{"id": "mappings", "value": [{"type": "value", "options": {"PAPER_FORWARD_TEST": {"text": "Paper forward test"}}}]}]})
    by_id[107]["fieldConfig"]["overrides"].append({"matcher": {"id": "byName", "options": "Reason"}, "properties": [{"id": "mappings", "value": [{"type": "value", "options": {"accounting_not_verified": {"text": "Accounting not verified"}, "disk_pressure": {"text": "Low free disk space"}, "strategy_realized_pnl_divergence:CRYPTO_SETTLEMENT_ENGINE": {"text": "Crypto PnL differs from the ledger"}}}]}]})
    # Three columns remain readable with Grafana's desktop navigation open.
    # Preserve the same logical ordering; use two compact rows instead of six
    # narrow tiles with clipped titles.
    for panel in p:
        old_y = panel["gridPos"]["y"]
        panel["gridPos"]["y"] += 2 * sum(old_y >= boundary for boundary in (6,17,30,49))
    for ids, start_y in ((range(101,107),2),(range(111,117),15),(range(121,127),30),(range(141,147),51)):
        for index, pid in enumerate(ids):
            by_id[pid]["gridPos"].update({"x": (index % 3) * 8, "y": start_y + (index // 3) * 3, "w": 8, "h": 3})
    return p


def diagnostics():
    runtime = [
        stat(161,"Supervisor","polymarket_v7_supervisor_alive",0,67,mapping={0:("DOWN","red"),1:("UP","green")}),
        stat(162,"Single Writer","polymarket_v7_single_writer_ok",4,67,mapping={0:("NOT VERIFIED","red"),1:("ONE OWNER","green")}),
        stat(163,"Exact SHA","polymarket_v7_exact_sha_ok",8,67,mapping={0:("DRIFT","red"),1:("MATCH","green")}),
        stat(164,"Kill Switch","polymarket_runtime_killed",12,67,source="portfolio",mapping={0:("DISENGAGED","green"),1:("ENGAGED","red")}),
        stat(165,"Restarts / Window","polymarket_v7_restart_count_window",16,67),
        stat(166,"Disk Free","polymarket_v7_disk_free_ratio",20,67,unit="percentunit",decimals=1),
        chart(167,"Runtime / component checks · not accounting verification",[("polymarket_v7_health","runtime contract"),("polymarket_v7_component_ready","{{component}}"),("polymarket_external_fair_present * polymarket_external_fair_healthy","crypto evidence feed")],0,71),
        table(168,"Engine reporting source · configured is not running","polymarket_v7_engine_reported_source_info",12,71,source="portfolio",columns={"engine":"Engine","source":"Reported source"}),
        stat(169,"Runtime uptime","polymarket_v7_runtime_uptime_seconds",0,79,w=6,unit="s"),
        stat(170,"Economic New-Risk Authority","polymarket_v7_economic_new_risk_ready",6,79,w=6,mapping={0:("BLOCKED · EXPECTED","yellow"),1:("AUTHORIZED","red")},description="Economic alpha authorization is separate from the frozen PAPER forward test. Blocked does not mean the forward test is stopped."),
        stat(171,"Configured algorithm count","polymarket_v7_live_algorithm_count",12,79,w=6),
        table(172,"Configured engines · not execution proof","polymarket_v7_economic_engine_configured",18,79,w=6),
        chart(173,"Ledger diagnostics · event counts, not unique opportunities",[("polymarket_execution_candidates","candidate events"),("polymarket_execution_makes","make events"),("polymarket_execution_takes","take events"),("polymarket_execution_arbs","arb events"),("polymarket_execution_cancels","cancel events"),("polymarket_execution_withdraws","withdraw events"),("polymarket_execution_effective_orders","effective-order events")],0,85),
        stat(175,"Raw portfolio reconciliation","polymarket_v7_portfolio_reconciled",0,93,w=12,mapping={0:("DIVERGED","red"),1:("MATCH","green")}),
        chart(174,"Accounting residuals · component state minus ledger",[("polymarket_v7_reconciliation_pnl_difference_usd","{{strategy}}")],12,85,unit="currencyUSD",decimals=3),
    ]
    universe = [
        chart(181,"Universe coverage · markets",[("polymarket_v7_universe_discovered_markets","discovered"),("polymarket_v7_universe_eligible_markets","eligible"),("polymarket_v7_universe_tier_markets","{{tier}}")],0,68),
        chart(182,"Discovery duration · milliseconds",[("polymarket_v7_universe_scan_duration_milliseconds","scan")],12,68,unit="ms",decimals=1),
        stat(183,"Exhaustive discovery","polymarket_v7_universe_discovery_exhaustive",0,76,w=6,mapping={0:("INCOMPLETE","red"),1:("EXHAUSTIVE","green")}),
        stat(184,"Pagination pages","polymarket_v7_universe_pages",6,76,w=6),
        table(185,"Resource limits · dimension shown explicitly","polymarket_v7_universe_resource_limit",12,76),
        chart(186,"Lead-lag label collection · observations",[("polymarket_v7_lead_lag_collector_origins","origins"),("polymarket_v7_lead_lag_collector_labels","labels"),("polymarket_v7_lead_lag_collector_nominal_horizon_eligible_labels","nominal eligible")],0,82,source="lead_lag_collector"),
    ]
    latency = [
        stat(191,"Measured latency available","polymarket_v7_latency_samples_present",0,69,w=8,mapping={0:("NO SAMPLES","yellow"),1:("OBSERVED","green")}),
        chart(192,"Internal stage p99 / p99.9 · not exchange ACK",[('polymarket_v7_latency_stage_nanoseconds{percentile=~"p99|p99_9"} / 1e6','{{stage}} {{percentile}}')],0,73,w=24,unit="ms",decimals=3,description="Only measured local stage samples. PAPER fills are not live exchange latency. No measurement is unavailable, never 0 ms."),
        table(193,"Markout sample counts","polymarket_execution_markout_observations",0,81,w=24),
    ]
    contexts = [
        table(201,"Crypto Settlement Contexts · registrations · NOT active trading","polymarket_v7_crypto_context_registered",0,70,w=24,description="Registered BTC/ETH/SOL/XRP contexts do not prove execution. Authority is listed per context."),
        chart(202,"Crypto authority flags",[("polymarket_v7_crypto_context_zero_authority","zero authority {{asset}} {{horizon}}"),("polymarket_v7_crypto_context_new_risk_authorized","new-risk permission {{asset}} {{horizon}}")],0,76),
        chart(203,"Coordinator crypto exposure · USD",[("polymarket_v7_crypto_gross_exposure_usd","gross"),("polymarket_v7_crypto_net_directional_exposure_usd","net directional"),("polymarket_v7_crypto_cluster_exposure_usd","correlated cluster")],12,76,unit="currencyUSD",decimals=2),
    ]
    for panel in runtime + universe + latency + contexts:
        panel["gridPos"]["y"] += 8
    return [row(160,"Details · runtime, ownership and accounting",74,runtime),row(180,"Details · discovery and research data quality",75,universe),row(190,"Details · latency and markout sample sizes",76,latency),row(200,"Details · crypto contexts and permissions",77,contexts)]


def common(dashboard):
    dashboard.update({"schemaVersion":39,"refresh":"10s","timezone":"Europe/Zurich","time":{"from":"now-1h","to":"now"},"editable":False,"graphTooltip":1})
    dashboard["templating"]={"list":[{"name":"instance","label":"Runtime target","type":"query","datasource":DS,"definition":'label_values(up{job="polymarket-v7"}, instance)',"query":{"query":'label_values(up{job="polymarket-v7"}, instance)',"refId":"instance"},"refresh":1,"sort":1,"multi":False,"includeAll":False,"current":{"selected":False,"text":"127.0.0.1:9108","value":"127.0.0.1:9108"},"options":[]}]}
    dashboard["links"]=[{"title":title,"type":"link","url":"/d/"+uid,"includeVars":True,"keepTime":True,"targetBlank":False} for title,uid in (("Control Room","polymarket-v7"),("Latency evidence","polymarket-v7-latency"),("Crypto evidence","polymarket-v7-external-fair")) if uid!=dashboard["uid"]]
    return dashboard


def main():
    destination=HERE/"grafana/dashboards/polymarket-v7.json"
    dashboard=common({"uid":"polymarket-v7","title":"Polymarket V7 — 24/7 PAPER Control Room","version":1,"tags":["polymarket","v7","paper","canonical-ledger","truth-v1"],"annotations":{"list":[]},"panels":build()+diagnostics()})
    destination.write_text(json.dumps(dashboard,indent=2,ensure_ascii=False)+"\n")


if __name__=="__main__":
    main()
