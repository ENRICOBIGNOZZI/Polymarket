"""Deterministic PAPER study reports, not SOTA or profitability certification.

Only the validated native runner supplies these inputs. Descriptive counts do
not establish sampling coverage, venue execution or economic stopping criteria.
No clocks, network calls, order authority or mutable latest-report pointers.
"""
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re

from v7_exact_arb_native_evidence import SAFETY, boundary, integer


DOCUMENTS = ("SOTA_GAP_ANALYSIS.md", "ECONOMIC_FUNNEL.md",
             "LATENCY_ATTRIBUTION.md", "SOTA_CHECKPOINT.md")
QUANTILES = {"p10": Fraction(1, 10), "p50": Fraction(1, 2),
             "p90": Fraction(9, 10), "p95": Fraction(19, 20),
             "p99": Fraction(99, 100), "p99.9": Fraction(999, 1000), "max": Fraction(1)}
STAGES = ("receive_to_decode_ns", "decode_to_graph_start_ns",
          "graph_start_to_relation_emit_ns", "receive_to_relation_emit_ns")


def health_snapshot(receipt_path, target_path, as_of_ms, model):
    """Attach canonical health without inventing current or historical uptime.

As-of is an explicit frozen report input, not the workstation clock. A matching
fresh snapshot is still only the supplied receipt's claim, not remote host or
deployment verification. Health never admits economic scenarios or changes risk.
    """
    if receipt_path is None and target_path is None and as_of_ms is None:
        return {"state": "UNVERIFIED", "reported_engineering_health": None,
                "reported_graph_health": None, "assessment_as_of_ms": None}, {}
    if receipt_path is None or target_path is None or type(as_of_ms) is not int or as_of_ms < 1:
        raise ValueError("health_receipt_target_and_as_of_required")
    artifacts = {}
    bodies = []
    for name,path,schema in (("runtime_health.json",receipt_path,"polymarket_v7_canonical_runtime_health_v1"),
                             ("runtime_target.json",target_path,"polymarket_v7_runtime_target_v1")):
        with Path(path).open("rb") as stream:
            raw = stream.read(1024*1024+1)
        if len(raw) > 1024*1024:
            raise ValueError("health_input_size")
        body = json.loads(raw)
        if (not isinstance(body,dict) or body.get("schema") != schema
                or any(body.get(k) is not v for k,v in SAFETY.items() if k != "execution_authority")
                or body.get("execution_authority",False) is not False):
            raise ValueError("health_input_schema_or_authority")
        artifacts[name] = raw; bodies.append(body)
    receipt,target = bodies
    instance = target.get("instance_id")
    region = target.get("region")
    if (not isinstance(instance,str) or not re.fullmatch(r"i-(?:[0-9a-f]{8}|[0-9a-f]{17})",instance)
            or not isinstance(region,str) or not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+",region)):
        raise ValueError("health_target_identity")
    stamp = integer(receipt,"timestamp_ms",1)
    checks = receipt.get("checks")
    if not isinstance(checks,dict) or not checks or any(type(v) is not bool for v in checks.values()):
        raise ValueError("health_canonical_checks")
    expected_health = "HEALTHY" if all(checks.values()) else "UNSAFE_OR_INCOMPLETE"
    if receipt.get("engineering_health") != expected_health:
        raise ValueError("health_inconsistent_check_summary")
    if receipt.get("graph_health") not in {"HEALTHY","UNAVAILABLE_OR_DEGRADED"}:
        raise ValueError("health_graph_state")
    reasons = []
    if not 0 <= as_of_ms-stamp <= 5000: reasons.append("STALE_OR_FUTURE_RECEIPT")
    if receipt.get("runtime_instance_id") != instance: reasons.append("WRONG_INSTANCE")
    az = receipt.get("runtime_az")
    if not isinstance(az,str) or not re.fullmatch(re.escape(region)+"[a-z]",az): reasons.append("WRONG_OR_UNKNOWN_AZ")
    if (not re.fullmatch(r"[0-9a-f]{40}",str(model)) or receipt.get("runtime_model_sha") != model
            or receipt.get("runtime_release_sha") != model): reasons.append("WRONG_OR_UNKNOWN_SHA")
    return {"state": "INELIGIBLE_SNAPSHOT" if reasons else "FRESH_MATCHING_CANONICAL_SNAPSHOT",
            "scope": "SUPPLIED_RECEIPT_AT_EXPLICIT_AS_OF_NOT_REMOTE_ATTESTATION_OR_STUDY_UPTIME",
            "assessment_as_of_ms": as_of_ms, "receipt_timestamp_ms": stamp,
            "freshness_limit_ms": 5000, "reasons": reasons,
            "runtime_instance_id": receipt.get("runtime_instance_id"),
            "runtime_az": receipt.get("runtime_az"), "runtime_model_sha": receipt.get("runtime_model_sha"),
            "runtime_release_sha": receipt.get("runtime_release_sha"),
            "reported_engineering_health": receipt["engineering_health"] if not reasons else None,
            "reported_graph_health": receipt["graph_health"] if not reasons else None,
            "current_health_verified": False,
            "limitation": "Health describes receipt collection time only; a source lease can expire before the report as-of.",
            "input_sha256": {k:hashlib.sha256(v).hexdigest() for k,v in artifacts.items()}}, artifacts


class LatencyEvidence:
    """Exact order statistics on disk, weighted by unique relation observation.

Graph start is frame-wide. Relation emit time includes preceding evaluations
and telemetry copies; it is deliberately NOT isolated per-relation compute.
Missing raw frames censor latency joins instead of fabricating stage times.
"""
    def __init__(self, db, archive):
        self.db, self.archive = db, archive
        self.missing = 0
        db.execute("CREATE TABLE latency(seq INTEGER PRIMARY KEY, frame INTEGER, "
                   "receive_to_decode_ns INTEGER, decode_to_graph_start_ns INTEGER, "
                   "graph_start_to_relation_emit_ns INTEGER, receive_to_relation_emit_ns INTEGER)")

    def ingest(self, row):
        # Caller has already validated and deduplicated the complete native row.
        frame = self.archive.db.execute("SELECT wire FROM replay WHERE seq=?",
                                       (row["feed_frame_sequence"],)).fetchone()
        if frame is None:
            self.missing += 1
            return
        frame = json.loads(frame[0])
        receive = integer(row, "receive_monotonic_ns", 1)
        start = integer(row, "decision_start_ns", 1)
        end = integer(row, "decision_end_ns", 1)
        decode = frame["availability_monotonic_ns"]
        if (receive != frame["receive_monotonic_ns"]
                or start != frame["graph_decision_start_ns"]
                or row["connection_epoch"] != frame["connection_epoch"]
                or frame["source_frame_valid"] is not True
                or not receive <= decode <= start <= end):
            raise ValueError("latency_frame_identity_or_clock")
        self.db.execute("INSERT INTO latency VALUES(?,?,?,?,?,?)",
                        (row["observation_sequence"], row["feed_frame_sequence"],
                         decode-receive, start-decode, end-start, end-receive))

    def finish(self):
        count, frames = self.db.execute("SELECT count(*), count(DISTINCT frame) FROM latency").fetchone()
        totals = [0]*len(STAGES)
        # Python integer totals avoid SQLite's signed-64 SUM overflow. Never
        # load the complete sample into memory or add marginal quantiles.
        for values in self.db.execute("SELECT "+",".join(STAGES)+" FROM latency"):
            totals = [a+b for a,b in zip(totals, values)]
        result = {}
        for i, stage in enumerate(STAGES):
            self.db.execute("CREATE INDEX latency_"+stage+" ON latency("+stage+")")
            quantiles = {}
            for label, fraction in QUANTILES.items():
                offset = fraction.numerator*(count-1)//fraction.denominator if count else 0
                quantiles[label] = self.db.execute("SELECT "+stage+" FROM latency ORDER BY "+stage+
                    " LIMIT 1 OFFSET ?", (offset,)).fetchone()[0] if count else None
            result[stage] = {"samples": count, "quantiles_ns": quantiles,
                            "total_ns": str(totals[i]) if count else None,
                            "paired_elapsed_fraction": str(Fraction(totals[i], totals[-1]))
                            if totals[-1] and i < 3 else None}
        return {"scope": "RECORDED_HOST_CLOCKS_NOT_LONDON_OR_VENUE_ATTESTATION",
                "population": "UNIQUE_RELATION_OBSERVATIONS_WITH_MATCHING_RAW_FRAME",
                "relation_observations": count, "distinct_frames": frames,
                "missing_raw_frame_observations": self.missing,
                "quantile_definition": "sorted_values[floor(p*(n-1))]",
                "contribution_definition": "sum(stage_ns)/sum(end_to_end_ns) on identical paired observations; not sum of quantiles",
                "stages": result,
                "unmeasured_segments": ["external_feed_to_host", "parser_vs_local_book_update",
                    "isolated_relation_compute", "strategy_to_risk", "risk_to_serialization",
                    "serialization_to_socket", "socket_to_exchange", "exchange_to_ack", "ack_to_fill"],
                "limitations": ["Receive-to-decode includes native parsing and book processing; these are not separately timed.",
                    "Graph start is frame-wide; emit elapsed includes preceding relations and telemetry work.",
                    "Decode/dispatch samples repeat for relations from the same frame; not independent frame samples.",
                    "Missing observations and unknown producer tail can bias the observed latency distribution.",
                    "Scenario arrival delays are assumptions, not measured transport latency."]}


def cell(value):
    if value is None:
        return "UNVERIFIED"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return str(value).replace("|", "&#124;").replace("\n", " ").replace("\r", " ")


def table(headers, rows):
    return "\n".join(["| "+" | ".join(headers)+" |", "| "+" | ".join("---" for _ in headers)+" |"]+
                     ["| "+" | ".join(cell(x) for x in row)+" |" for row in rows])+"\n"


def render(report):
    """Render one immutable study; never select a strategy or infer absent data."""
    boundary(report)
    if report.get("schema") != "polymarket_v7_native_exact_arb_study_v1":
        raise ValueError("sota_report_schema")
    # This report version has no approved economic-decision inputs. Fail rather
    # than overwrite a new decision schema with a misleading old template.
    if any(value is not None for value in report["economic_evidence"].values()):
        raise ValueError("sota_economic_evidence_requires_new_contract")
    common = (f"Study: `{report['study_id']}`. Recorded model SHA: `{report['model_sha']}`.\n\n"
              f"Session: {cell(report['observer_session_id'])}. PAPER / zero authority.\n\n"
              "Automatically generated from this directory's `report.json` and hash-bound evidence. "
              "This is a recorded-session analysis, not current London health, full CI certification or a profitability claim. "
              "Missing data means UNVERIFIED, never zero.\n\n")
    diagnostics = report["diagnostics"]
    quality = report["data_quality"]
    health = report["runtime_health_snapshot"]
    latency = report["latency_attribution"]
    families = diagnostics["families"]
    gaps = [
        ("Native raw replay", "RECORDED", "Complete supplied replay validated", "data_quality.native_replay_validated="+str(quality["native_replay_validated"]),
         "Producer tail and graph-decision re-execution not certified", "Reconcile producer counters and independently replay graph decisions"),
        ("Diagnostic relations", "RECORDED", "Causal adequately observed relation population", str(diagnostics["engineering_evidence"].get("evaluations", 0))+" retained evaluations",
         "Update-weighted diagnostics are not independent opportunities", "Preserve episode censoring and cluster by event/time"),
        ("Universe / semantics", "UNVERIFIED", "Broad independently verified payoff coverage", str(len(families))+" diagnostic family/direction groups; exchange coverage unknown",
         "No coverage weights or independent NegRisk completeness attestation in study", "Verify settlement semantics before enabling new relations"),
        ("Execution", "SCENARIO_ONLY", "Calibrated arrival, matching, ACK/cancel and unwind", "arms and shared_execution; separate alternative worlds",
         "Public L2 simulation and recorded metadata leases do not attest venue behavior or atomic match-time fee freshness", "Require exact admitted receipt provenance; verify remaining fee/matching semantics"),
        ("Control-plane continuity", report["control_history"]["state"], "No fills after observed metadata invalidation", report["control_history"],
         "Only recorded local source-admission intervals; not independent venue semantics", "Preserve all control transitions and checkpoints alongside causal books"),
        ("Sizing / capital", "MODEL_LIFECYCLE_ONLY", "Global q*, champion parity, verified inventory and release", "capital_plans/transitions.jsonl; separate no-release resource_plans baseline",
         "Known modeled ACK outcomes release excess reservations, not settlement payout; fees, matching and economic capacity remain unverified", "Verify venue accounting, retained inventory settlement and capacity under observed depth"),
        ("Maker / markout", "UNVERIFIED", "Calibrated paired fill and adverse-selection economics", None,
         "No calibrated maker queue evidence in this study", "Separate maker forward calibration from locked taker PnL"),
        ("Latency", "PARTIALLY_MEASURED", "Full segmented latency with champion non-regression", str(latency["relation_observations"])+" paired host-clock observations",
         "Transport/ACK and isolated relation compute unmeasured", "Measure missing stages and actual champion under side-by-side load"),
        ("Runtime / release", health["state"], "Fresh canonical identity health and exact-SHA official release", health,
         "Optional receipt is not remote attestation, study uptime or clean release CI", "Obtain canonical runtime receipt and complete official release gates"),
        ("Reporting", "OFFLINE_HOURLY_WINDOWS", "Reproducible reports plus hourly live evidence", "Four documents and hash-bound hourly.jsonl",
         "Hourly multi-session service not established; session monotonic hours are not UTC or observed uptime", "Wire bounded hourly orchestration with session/clock provenance without pooling alternate arms"),
        ("Economic decision", "INSUFFICIENT_EVIDENCE", "Predeclared bounded causal economic test", "economic_evidence.research_decision=null",
         "No sufficient coverage/exposure, after-cost portfolio PnL or cluster uncertainty", "Freeze protocol, collect 8–48h qualifying evidence, apply stopping criteria"),
    ]
    gap = "# SOTA gap analysis\n\n"+common+table(
        ["Component", "Current", "Target", "Evidence", "Gap", "Action"], gaps)
    funnel = "# Economic funnel\n\n"+common
    funnel += "## Diagnostic evaluations\n\nThese are relation updates, not trades. Readiness counts overlap; they are not a sequential conversion funnel. Raw/fee/reserve distances are top-of-book diagnostics, not full-depth executable PnL.\n\n"
    fields = ("evaluations", "books_ready", "fees_ready", "lineage_ready", "freshness_ready", "leg_skew_ready",
              "raw_positive_evaluations", "after_fee_positive_evaluations", "after_reserve_positive_evaluations",
              "depth_complete", "accepted_evaluations", "sizing_model_evaluations", "sizing_model_optima_proven",
              "sizing_search_exhausted", "sizing_unknown_evaluations")
    funnel += table(["Family/direction", *fields],
                    [(name, *(values["evaluation_funnel"].get(k) for k in fields)) for name,values in sorted(families.items())])
    funnel += "\n## Episodes and censoring\n\nKnown negative-to-positive starts only; left-censored segments do not establish a new arrival. Missing full evidence censors a scenario, not its economic loss.\n\n"
    funnel += table(["Family/direction", "Stage", "Observed starts", "Complete diagnostic lifetimes", "Censored segments"],
        [(name, stage, values["evaluation_funnel"].get(stage+"_observed_starts"),
          life["complete_episodes"], life["censored_segments"])
         for name,values in sorted(families.items()) for stage,life in sorted(values["lifetimes"].items())])
    funnel += "\n## Near-arbitrage distance\n\nExact descriptive, update-weighted quantiles; no independence or opportunity-rate inference. Units and quantile probabilities are explicit.\n\n"
    funnel += table(["Family/direction", "Stage", "Samples", "PUSD quantiles", "Tick quantiles", "bps quantiles", "Fraction within ticks"],
        [(name,stage,m["observations"],m["quantiles"]["pusd"],m["quantiles"]["ticks"],m["quantiles"]["bps"],m["fraction_within_ticks"])
         for name,values in sorted(families.items()) for stage,m in sorted(values["near_arbitrage"].items())])
    funnel += "\n## Independent counterfactual arms\n\nDo not add arms or interpret full fills as observed venue fills. Positive locked PnL is conditional, not realized cash profit.\n\n"
    funnel += "No configured venue-hold profile means an unverified zero-hold upper bound, not a claim that these markets match immediately. Explicit per-token holds and response times are scenario assumptions, not causal metadata attestations.\n\n"
    funnel += "With REQUIRE_RECORDED_SELECTION_TERMS, holds instead come from each candidate's archived native selection; missing, mismatched or expired provenance censors the scenario. Receipt integrity is not a venue execution guarantee.\n\n"
    funnel += table(["Arm", "Mode", "Transport ms", "Token holds ms", "Response ms", "Scenarios", "States", "Partial-fill scenarios", "Conditional positive locked-PnL scenarios"],
        [(a["arm_id"],a["mode"],a["delay_ms"],a.get("venue_delay_ms_by_token") if a.get("venue_delay_ms_by_token") is not None else
          "PER_CANDIDATE_ADMITTED_RECEIPTS" if report["config"].get("venue_terms_mode")=="REQUIRE_RECORDED_SELECTION_TERMS" else
          "ZERO_HOLD_UPPER_BOUND_UNVERIFIED",a.get("ack_delay_ms"),a["scenario_count"],a["states"],a["partial_fill_scenarios"],
          a["conditional_positive_locked_pnl_scenarios"]) for a in report["arms"]])
    funnel += "\n## Shared liquidity worlds\n\nEach capital/latency world is a separate alternative. Decision-time allocation interleaves modeled ACK outcomes; only fully reconciled outcomes release excess funding. Reserve stays encumbered; acquired tokens never become collateral automatically. Censored, residual and transformation outcomes retain their holds. These hypothetical balances and fixed quantities are not a verified capacity curve.\n\n"
    funnel += "`resource_plans.jsonl` is the independent no-release planning baseline. `capital_plans.jsonl` and `capital_transitions.jsonl` describe the event-ordered model lifecycle actually used by shared scenarios. Closed-unwind model PnL excludes unsettled inventory and unresolved orders; it is not portfolio profit.\n\n"
    funnel += table(["Capital PUSD", "Arm", "Receipt"],
        [(w["hypothetical_paper_capital"],w["arm_id"],w) for w in report["shared_execution"]["worlds"]])
    funnel += "\nConservative portfolio net PnL, opportunity/hour, capacity, causal exchange coverage and dominant economic root cause: UNVERIFIED. No STOP/CONTINUE decision is justified by these diagnostics alone.\n"
    funnel += "\n## Hourly attribution\n\n`hourly.jsonl` preserves whole-session episode state across hour boundaries. Windows use the session's monotonic clock, not verified UTC or uptime. Diagnostics are attributed when emitted; model results when known. Their populations differ and must not be divided into a same-hour conversion rate. Empty windows mean no recorded rows, not observed zero opportunities. Wall-clock ranges are reported metadata only. Loss counters identify when gaps were detected, not the unknown loss instant. Runtime health, exchange coverage and economic rates remain unknown.\n"
    timing = "# Latency attribution\n\n"+common
    timing += f"Population: {latency['population']}. Distinct frames: {latency['distinct_frames']}; paired relation observations: {latency['relation_observations']}; missing raw-frame joins: {latency['missing_raw_frame_observations']}.\n\n"
    timing += table(["Recorded stage", "Samples", *QUANTILES, "Fraction of paired elapsed total"],
        [(stage,m["samples"],*(m["quantiles_ns"][q] for q in QUANTILES),m["paired_elapsed_fraction"])
         for stage in STAGES for m in [latency["stages"][stage]]])
    timing += "\nAll quantiles are nanoseconds. "+latency["quantile_definition"]+".\n\n"+latency["contribution_definition"]+".\n\n"
    timing += "\n".join("- "+text for text in latency["limitations"])+"\n\n"
    timing += table(["Unmeasured segment", "Status"], [(stage,"UNVERIFIED") for stage in latency["unmeasured_segments"]])
    timing += "\nTransport, per-token venue holds and response sensitivity arms are in ECONOMIC_FUNNEL.md. Their assumed delays are not measured London transport times, contemporaneous venue attestations or a verified latency-value curve.\n"
    checkpoint = "# SOTA checkpoint\n\n"+common
    checkpoint += table(["Item", "Evidence / status"], [
        ("Recorded model SHA", report["model_sha"]), ("Session manifest SHA256", report["session_manifest_sha256"]),
        ("Raw replay output chain SHA256", report["replay_receipt"]["output_chain_sha256"]),
        ("Diagnostic input SHA256", diagnostics["normalized_input_sha256"]),
        ("Full-evidence input SHA256", report["normalized_full_evidence_sha256"]),
        ("Source hashes", report["runner_source_sha256"]), ("Frozen study config", report["config"]),
        ("Canonical health snapshot at explicit as-of", health),
        ("Deployment / current runtime", None), ("Exact-source clean release CI", None),
        ("Data quality", quality), ("Episode admission", report["episode_admission"]),
        ("Engineering health", "Recorded replay validated; current runtime UNVERIFIED"),
        ("Economic evidence", "INSUFFICIENT_EVIDENCE; research_decision=null"),
        ("Open issues", "See SOTA_GAP_ANALYSIS.md; report generation does not close its gaps"),
        ("Control-plane evidence", report["control_history"]),
        ("Next highest-value action", "Verify independent execution terms and capital lifecycle before frozen live economic inference")])
    checkpoint += "\n## Limitations\n\n"+"\n".join("- "+text for text in report["limitations"])+"\n"
    return dict(zip(DOCUMENTS, (gap, funnel, timing, checkpoint)))
