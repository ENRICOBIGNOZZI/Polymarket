"""Offline, zero-authority native episode/execution evidence runner.

Input replay JSONL must come from polymarket_v7_exact_arb_ws_replay and end in
its success receipt. A first pass validates the ENTIRE replay before a second
pass exposes verified prefixes to a bounded arrival history. No positive-only
tape is used as arrival history. Output is immutable and never summed as a
capital-feasible portfolio. Alternative scenario arms remain separate.
"""
from collections import Counter
from fractions import Fraction
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile

from v7_exact_arb_native_arrival import NativeArrivalHistory, require
from v7_exact_arb_native_evidence import SAFETY, SCHEMA, Evidence, boundary, canonical, integer
from v7_exact_arb_native_execution_bridge import native_candidate
from v7_exact_arb_resource_scheduler import ShadowResourceJournal, ShadowCapitalJournal, native_request, rational
from v7_exact_arb_shared_execution import SharedExecutionQueue, NativeLiquidityFeed
from v7_exact_arb_control_history import ControlHistory
from v7_exact_arb_sota_report import LatencyEvidence, health_snapshot, render as render_sota
from v7_exact_arb_hourly import HourlyStudy
from v7_unified_exact_arb_graph import GraphError, frac, fstr, sha
from v7_unified_exact_arb_graph_execution_shadow import simulate
from v7_exact_arb_execution_timing import timing_plan, normalize_delay_profile, duration, candidate_delay_profile


class InputBudget:
    def __init__(self, maximum_rows=20000000, maximum_bytes=4*1024**3):
        require(type(maximum_rows) is int and maximum_rows>0 and type(maximum_bytes) is int and maximum_bytes>0, "input_budget")
        self.maximum_rows,self.maximum_bytes=maximum_rows,maximum_bytes
        self.rows=self.bytes=0
        self.guard=None

    def lines(self, paths):
        for path in paths:
            with Path(path).open("rb") as stream:
                while True:
                    raw=stream.readline(8*1024*1024+1)
                    if not raw: break
                    self.rows+=1;self.bytes+=len(raw)
                    require(len(raw)<=8*1024*1024 and raw.endswith(b"\n"), "unsealed_or_oversize_input")
                    require(self.rows<=self.maximum_rows and self.bytes<=self.maximum_bytes, "input_budget_exceeded")
                    if self.guard: self.guard()
                    # Preserve exact bytes in the native output hash chain.
                    yield raw[:-1].decode("utf-8")


class ReplayArchive:
    """Private temporary disk archive; only validated native output is admitted."""
    def __init__(self, database, manifest, model, replay, budget, max_frames, max_bytes):
        self.manifest,self.model=manifest,model
        self.max_frames,self.max_bytes=max_frames,max_bytes
        self.db=sqlite3.connect(database)
        self.db.execute("CREATE TABLE replay(seq INTEGER PRIMARY KEY, available INTEGER, wire TEXT, chain TEXT)")
        self.db.execute("CREATE INDEX replay_availability ON replay(available,seq)")
        history=None
        self.receipt=None
        try:
            history=NativeArrivalHistory(manifest,model,max_frames=max_frames,max_bytes=max_bytes)
            for wire in budget.lines([replay]):
                require(self.receipt is None, "rows_after_replay_receipt")
                row=json.loads(wire)
                if row.get("schema")=="polymarket_v7_native_exact_arb_ws_replay_receipt_v1":
                    history.seal(wire);self.receipt=row
                else:
                    history.ingest(wire)
                    self.db.execute("INSERT INTO replay VALUES(?,?,?,?)",
                        (history.sequence,history.available,wire,history.chain))
            require(self.receipt is not None, "missing_replay_receipt")
            self.db.commit()
        except Exception:
            self.db.close()
            raise
        finally:
            if history is not None: history.close()

    def close(self):
        self.db.close()

    def history(self):
        return VerifiedPrefixHistory(self)


class VerifiedPrefixHistory(NativeArrivalHistory):
    """Replay already validated data, without sealing/unsealing fake receipts.

    Every prefix digest must equal the first pass's digest. Admission binds the
    independently checked terminal receipt, not a caller-supplied `verified`
    boolean. Prefixes are available only from the private archive iterator.
    """
    def __init__(self, archive):
        super().__init__(archive.manifest,archive.model,max_frames=archive.max_frames,max_bytes=archive.max_bytes)
        self.archive=archive
        self.validated_prefix=None

    def advance(self):
        for sequence,available,wire,chain in self.archive.db.execute("SELECT seq,available,wire,chain FROM replay ORDER BY seq"):
            self.validated_prefix=None
            self.ingest(wire)
            require(self.sequence==sequence and self.available==available and self.chain==chain, "archive_changed")
            self.validated_prefix=(self.sequence,self.chain)
            yield sequence
        self.seal(canonical(self.archive.receipt))

    def receipt_verified(self):
        return not self.failed and self.validated_prefix==(self.sequence,self.chain)

    def evidence_identity(self):
        result=super().evidence_identity()
        result.update(complete_replay_output_chain_sha256=self.archive.receipt["output_chain_sha256"],
            complete_replay_frames=self.archive.receipt["frames_replayed"],
            scope="VERIFIED_PREFIX_OF_FULLY_VALIDATED_NATIVE_REPLAY")
        return result


def normalize_arms(arms):
    require(isinstance(arms,list) and 0<len(arms)<=64,"scenario_arm_budget")
    result=[];seen=set()
    for raw in arms:
        required={"mode","delay_ms","skew_ms","unwind_delay_ms","order_type"}
        require(isinstance(raw,dict) and required<=set(raw) and
                set(raw)<=required|{"venue_delay_ms_by_token","ack_delay_ms"}, "scenario_arm_fields")
        require(raw["mode"] in {"SEQUENTIAL","PARALLEL","BATCH"} and raw["order_type"] in {"FAK","FOK"}, "scenario_arm_mode")
        row={**raw}
        for key in ("delay_ms","skew_ms","unwind_delay_ms"):
            value=frac(raw[key]);require(0<=value<=1000 and (value*1000000).denominator==1, "scenario_arm_time")
            row[key]=fstr(value)
        row["venue_delay_ms_by_token"]=normalize_delay_profile(raw.get("venue_delay_ms_by_token"))
        row["ack_delay_ms"]=fstr(duration(raw.get("ack_delay_ms",0)))
        identifier=sha(row)
        require(identifier not in seen,"duplicate_scenario_arm")
        seen.add(identifier);result.append({**row,"arm_id":identifier})
    return sorted(result,key=lambda arm:arm["arm_id"])


DEFAULT_ARMS=[{"mode":"PARALLEL","delay_ms":str(delay),"skew_ms":"0","unwind_delay_ms":"2","order_type":"FAK"}
              for delay in (1,2,5,10,25,50,100)]


def finish_ns(candidate,arm):
    try:
        plan=timing_plan([leg["token_id"] for leg in candidate["relation"]["legs"]],candidate["timestamp_ms"],
            arm["mode"],arm["delay_ms"],arm["skew_ms"],arm["unwind_delay_ms"],
            candidate_delay_profile(candidate,arm.get("venue_delay_ms_by_token")),arm.get("ack_delay_ms",0))
        finish=plan["finish_ms"]
    except GraphError as error:
        # An incomplete explicit profile produces a censored scenario, not a
        # fabricated zero-delay schedule or the loss of all other study arms.
        if str(error)!="execution_mandatory_delay_unknown" and not str(error).startswith("venue_candidate_"): raise
        finish=frac(candidate["timestamp_ms"])
    ns=finish*1000000
    require(ns.denominator==1 and 0<ns<2**63, "scenario_horizon")
    return ns.numerator


def input_identity(row, model, session, manifest_hash):
    boundary(row)
    require(row.get("model_sha")==model and row.get("observer_session_id")==session
            and row.get("session_manifest_sha256")==manifest_hash,"run_input_identity")


def write_rows(db, query, path, guard):
    digest=hashlib.sha256()
    with path.open("wb") as stream:
        for index,(body,) in enumerate(db.execute(query)):
            raw=(body+"\n").encode();stream.write(raw);digest.update(raw)
            if index%64==0: stream.flush();guard()
    return digest.hexdigest()


def file_digest(path):
    digest=hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b""): digest.update(chunk)
    return digest.hexdigest()


def source_hashes():
    root=Path(__file__).parent
    names=("v7_exact_arb_native_run.py","v7_exact_arb_native_arrival.py","v7_exact_arb_native_execution_bridge.py",
           "v7_exact_arb_native_evidence.py","v7_unified_exact_arb_graph_execution_shadow.py",
           "v7_unified_exact_arb_graph.py","v7_exact_relation_discovery.py","v7_exact_arb_resource_scheduler.py",
           "v7_exact_arb_causal.py","v7_exact_arb_shared_execution.py","v7_exact_arb_sota_report.py",
           "v7_exact_arb_control_history.py","v7_exact_arb_execution_timing.py",
           "v7_exact_arb_venue_selection.py","v7_exact_arb_venue_terms.py","v7_exact_arb_hourly.py")
    return {name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names}


def run(*, manifest, replay, observations, full_evidence, bundles, model, output, arms=None,
        max_frames=4096, max_history_bytes=64*1024*1024, maximum_rows=20000000, maximum_bytes=4*1024**3,
        maximum_working_bytes=8*1024**3, capital_budgets=("1000","10000","100000"),
        runtime_health=None, runtime_target=None, report_as_of_ms=None, control_events=None, require_venue_terms=False):
    """One immutable session study; frozen config, no risk/execution authority."""
    arms=normalize_arms(DEFAULT_ARMS if arms is None else arms)
    require(type(require_venue_terms) is bool,"venue_terms_mode")
    require(isinstance(capital_budgets,(list,tuple)) and 0<len(capital_budgets)<=16,"capital_budget_count")
    capital_budgets=sorted({rational(value) for value in capital_budgets})
    require(all(value>0 for value in capital_budgets),"capital_budget_value")
    require(len(capital_budgets)*len(arms)<=128,"shared_world_budget")
    health,health_artifacts=health_snapshot(runtime_health,runtime_target,report_as_of_ms,model)
    config={"max_history_frames":max_frames,"max_history_bytes":max_history_bytes,
            "maximum_input_rows":maximum_rows,"maximum_input_bytes":maximum_bytes,"maximum_working_bytes":maximum_working_bytes,
            "hypothetical_paper_capital_budgets":[fstr(value) for value in capital_budgets],
            "maximum_shared_pending_jobs":4096,"maximum_shared_pending_serialized_bytes":64*1024*1024,
            "shared_capital_policy":ShadowCapitalJournal.POLICY,
            "capital_release_tie_policy":"STRICTLY_AFTER_MODELED_RESULT_ONE_NS_NOT_MEASURED_VENUE_RELEASE",
            "health_snapshot_inputs":{"as_of_ms":report_as_of_ms,"sha256":health.get("input_sha256")},
            "venue_terms_mode":"REQUIRE_RECORDED_SELECTION_TERMS" if require_venue_terms else "UNVERIFIED_SCENARIOS"}
    budget=InputBudget(maximum_rows,maximum_bytes)
    require(type(maximum_working_bytes) is int and maximum_working_bytes>0,"working_budget")
    manifest_path=Path(manifest)
    require(manifest_path.stat().st_size<=1024*1024,"manifest_size")
    manifest_bytes=manifest_path.read_bytes();manifest_body=json.loads(manifest_bytes)
    session=manifest_body.get("observer_session_id");manifest_hash=hashlib.sha256(manifest_bytes).hexdigest()
    code=source_hashes()
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="native-run-",dir=output) as temporary:
        root=Path(temporary)
        def guard():
            # Offline scratch files only. Never delete evidence or other data
            # to make room. Fail before publishing if working storage is unsafe.
            total=sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
            require(total<=maximum_working_bytes,"working_budget_exceeded")
            require(shutil.disk_usage(root).free>=128*1024*1024,"insufficient_free_disk")
        budget.guard=guard
        guard()
        archive=ReplayArchive(root/"replay.sqlite",manifest_bytes,model,replay,budget,max_frames,max_history_bytes)
        evidence=None;db=None;history=None;capital_journals={}
        try:
            evidence=Evidence(root/"diagnostics.sqlite",bundles,model)
            db=sqlite3.connect(root/"study.sqlite")
            db.executescript("""
                CREATE TABLE full_rows(seq INTEGER PRIMARY KEY, digest TEXT, body TEXT);
                CREATE TABLE candidates(id TEXT PRIMARY KEY, body TEXT);
                CREATE TABLE tasks(id TEXT, arm INTEGER, finish INTEGER, done INTEGER DEFAULT 0, PRIMARY KEY(id,arm));
                CREATE INDEX pending ON tasks(done,finish,id,arm);
                CREATE TABLE admissions(id TEXT PRIMARY KEY, body TEXT);
                CREATE TABLE cycles(id TEXT PRIMARY KEY, arm INTEGER, body TEXT);
                CREATE TABLE resource_requests(id TEXT PRIMARY KEY, available INTEGER, body TEXT);
                CREATE INDEX request_time ON resource_requests(available,id);
                CREATE TABLE resource_plans(id TEXT PRIMARY KEY, body TEXT);
                CREATE TABLE shared_members(world TEXT,id TEXT,available INTEGER,added INTEGER DEFAULT 0,PRIMARY KEY(world,id));
                CREATE INDEX shared_admission ON shared_members(added,available,id,world);
                CREATE TABLE shared_cycles(id TEXT PRIMARY KEY,world TEXT,body TEXT);
                CREATE TABLE capital_plans(id TEXT PRIMARY KEY,body TEXT);
                CREATE TABLE capital_transitions(id TEXT PRIMARY KEY,body TEXT);
            """)
            control=ControlHistory(db,budget.lines(control_events or []),model,session,manifest_hash)
            config["control_history"]=control.receipt()
            counts=Counter({key:0 for key in ("duplicate_full_rows_removed","full_rows_without_diagnostic_row",
                "left_censored_segments_excluded","observed_episode_starts")})
            latency=LatencyEvidence(db,archive)
            hourly=HourlyStudy(db,archive)
            for wire in budget.lines(observations):
                row=json.loads(wire);input_identity(row,model,session,manifest_hash)
                integer(row,"feed_frame_sequence",1)
                previous_missing=evidence.counts["missing_observation_rows"]
                increment=evidence.ingest(row)
                if increment is not None:
                    latency.ingest(row)
                    family,counters=increment
                    hourly.ingest(row,family,counters,evidence.bundles.relation(row),
                                  evidence.counts["missing_observation_rows"]-previous_missing)
            diagnostics=evidence.finish()
            latency_attribution=latency.finish()
            full_sequence=0
            full_hash=hashlib.sha256()
            for wire in budget.lines(full_evidence):
                row=json.loads(wire);input_identity(row,model,session,manifest_hash)
                require(row.get("schema")=="polymarket_v7_native_exact_arb_full_evidence_v2", "full_schema")
                sequence=integer(row,"observation_sequence",1)
                integer(row,"feed_frame_sequence",1)
                encoded=canonical(row);digest=hashlib.sha256(encoded.encode()).hexdigest()
                existing=db.execute("SELECT digest FROM full_rows WHERE seq=?",(sequence,)).fetchone()
                if existing:
                    require(existing[0]==digest,"conflicting_full_duplicate")
                    counts["duplicate_full_rows_removed"]+=1;continue
                require(sequence>full_sequence,"full_sequence_reversal")
                full_sequence=sequence
                # Full rows and diagnostic rows are copies of the same native
                # observation. Independent bounded queues may lose either one.
                base={k:v for k,v in row.items() if k!="leg_books"};base["schema"]=SCHEMA
                observed=evidence.db.execute("SELECT digest FROM seen WHERE session=? AND sequence=?",(session,sequence)).fetchone()
                if observed is not None:
                    require(observed[0]==hashlib.sha256(canonical(base).encode()).hexdigest(),"full_observation_mismatch")
                else: counts["full_rows_without_diagnostic_row"]+=1
                db.execute("INSERT INTO full_rows VALUES(?,?,?)",(sequence,digest,encoded))
                full_hash.update((digest+"\n").encode())
            venue_input_hash=hashlib.sha256()
            for (body,) in evidence.db.execute("SELECT payload FROM episodes WHERE stage='pre_allocation' ORDER BY id"):
                episode=json.loads(body)
                if episode["left_censored"]:
                    counts["left_censored_segments_excluded"]+=1;continue
                counts["observed_episode_starts"]+=1
                record={**SAFETY,"episode_id":episode["episode_id"],"model_sha":model,"family":episode["family"],
                    "state":"CENSORED","reason":None,"first_positive_ns":episode["first_positive_ns"]}
                full=db.execute("SELECT body FROM full_rows WHERE seq=?",(episode["first_observation_sequence"],)).fetchone()
                if full is None:
                    record["reason"]="missing_full_evidence"
                else:
                    try:
                        candidate=native_candidate(json.loads(full[0]),episode,evidence.bundles,model,require_venue_terms=require_venue_terms)
                    except GraphError as error:
                        if str(error)!="native_decision_expired": raise
                        candidate=None;record["reason"]=str(error);counts["decision_expired_episodes"]+=1
                    if candidate is not None:
                        venue_input_hash.update((canonical([episode["episode_id"],candidate.get("venue_terms")])+"\n").encode())
                        if require_venue_terms:
                            counts["venue_terms_recorded_candidates" if candidate["venue_terms"]["state"]=="RECORDED_PUBLIC_TERMS"
                                   else "venue_terms_unavailable_candidates"]+=1
                        if candidate["result"]["direction"]=="SELL":
                            record["reason"]="inventory_reservation_unverified"
                        else:
                            record.update(state="SCENARIOS_SCHEDULED",reason=None)
                            db.execute("INSERT INTO candidates VALUES(?,?)",(episode["episode_id"],canonical(candidate)))
                            resource_request=native_request(candidate)
                            db.execute("INSERT INTO resource_requests VALUES(?,?,?)",(episode["episode_id"],
                                resource_request["decision_available_ns"],canonical(resource_request)))
                            for index,arm in enumerate(arms):
                                db.execute("INSERT INTO tasks(id,arm,finish) VALUES(?,?,?)",
                                           (episode["episode_id"],index,finish_ns(candidate,arm)))
                db.execute("INSERT INTO admissions VALUES(?,?)",(episode["episode_id"],canonical(record)))
                guard()
            db.commit()
            config["venue_terms_input_sha256"]=venue_input_hash.hexdigest()
            history=archive.history()
            study_id=sha([model,manifest_hash,archive.receipt,diagnostics["normalized_input_sha256"],
                          full_hash.hexdigest(),config,arms,code])
            world_terms={}

            # A separate causal planning sensitivity, NEVER an execution-PnL
            # filter. It sees decision-time requests only, not scenario outcomes.
            for capital in capital_budgets:
                for arm in arms:
                    world_id=sha([study_id,fstr(capital),arm,ShadowCapitalJournal.POLICY])
                    world_terms[world_id]=(capital,arm)
                journal=ShadowResourceJournal(root/("resources-"+sha(fstr(capital))+".sqlite"),model)
                try:
                    for (when,) in db.execute("SELECT DISTINCT available FROM resource_requests ORDER BY available"):
                        batch=[];capacities={"PUSD":fstr(capital)}
                        for (body,) in db.execute("SELECT body FROM resource_requests WHERE available=? ORDER BY id",(when,)):
                            request=json.loads(body);batch.append(request)
                            require(len(batch)<=64,"resource_batch_capacity")
                            for key,value in request["depth_capacities"].items():
                                require(key not in capacities or capacities[key]==value,"conflicting_decision_depth_capacity")
                                capacities[key]=value
                        snapshot={**SAFETY,"schema":"polymarket_v7_exact_arb_shadow_capacity_v1","model_sha":model,
                            "capacity_basis":"TOTAL_BEFORE_RESEARCH_HOLDS_NOT_EXECUTION_AUTHORITY",
                            "available_ns":when,"expires_ns":min(row["valid_until_ns"] for row in batch),"capacities":capacities}
                        identifier=sha([study_id,fstr(capital),when])
                        plan=journal.admit(identifier,batch,snapshot,when)
                        plan.update(hypothetical_paper_capital=fstr(capital),study_id=study_id,
                            planning_scope="DECISION_TIME_HOLDS_NO_RELEASE_NOT_EXECUTION_CAPACITY_CURVE")
                        db.execute("INSERT INTO resource_plans VALUES(?,?)",(identifier,canonical(plan)));guard()
                finally: journal.close()
            db.commit()
            worlds={};pending_keys=set();pending_candidates={};pending_bytes=0;peak_pending=0

            def shared_result(row):
                nonlocal pending_bytes
                key=(row["shared_world_id"],row["opportunity_id"])
                if key in pending_keys:
                    pending_keys.remove(key)
                    cached=pending_candidates[key[1]]
                    world=worlds[key[0]]
                    # The result is available strictly AFTER its modeled ACK;
                    # same-time decision batches cannot spend its release.
                    stamp=max(world.now,frac(cached["candidate"]["timestamp_ms"]))*1000000
                    require(stamp.denominator==1,"capital_result_time_precision")
                    known_ns=int(stamp)+1
                    if row["state"]=="CENSORED":
                        # A terminal missing-outcome diagnosis is known at the
                        # last replay watermark, not its unobserved future ACK.
                        known_ns=min(known_ns,history.available)
                    transition=capital_journals[key[0]].complete(cached["candidate"],row,known_ns)
                    db.execute("INSERT INTO capital_transitions VALUES(?,?)",
                               (row["shared_execution_id"],canonical(transition)))
                    cached["references"]-=1
                    if cached["references"]==0:
                        pending_bytes-=cached["bytes"];del pending_candidates[key[1]]
                db.execute("INSERT INTO shared_cycles VALUES(?,?,?)",(row["shared_execution_id"],key[0],canonical(row)))
                guard()

            for world_id,(capital,arm) in sorted(world_terms.items()):
                capital_journals[world_id]=ShadowCapitalJournal(root/("capital-"+world_id+".sqlite"),model,world_id,{"PUSD":fstr(capital)})
                world=SharedExecutionQueue(arm,world_id,shared_result)
                liquidity=NativeLiquidityFeed(archive,world.depletion)
                world.before_event=liquidity.advance
                worlds[world_id]=world

            def add_shared_members(through):
                nonlocal pending_bytes,peak_pending
                while True:
                    pending=db.execute("SELECT world,id FROM shared_members WHERE added=0 AND available<? "
                        "ORDER BY available,id,world LIMIT 1",(through,)).fetchone()
                    if pending is None: break
                    world_id,identifier=pending
                    require(len(pending_keys)<config["maximum_shared_pending_jobs"],"shared_pending_budget")
                    if identifier not in pending_candidates:
                        body=db.execute("SELECT body FROM candidates WHERE id=?",(identifier,)).fetchone()[0]
                        size=len(body.encode())
                        require(pending_bytes+size<=config["maximum_shared_pending_serialized_bytes"],"shared_pending_budget")
                        candidate=json.loads(body)
                        try: view=control.view(history.for_candidate(candidate),candidate);error=None
                        except GraphError as exc: view=None;error=str(exc)
                        pending_candidates[identifier]={"candidate":candidate,"view":view,"error":error,"bytes":size,"references":0}
                        pending_bytes+=size
                    cached=pending_candidates[identifier];world=worlds[world_id]
                    cached["references"]+=1;pending_keys.add((world_id,identifier))
                    peak_pending=max(peak_pending,len(pending_keys))
                    if cached["error"] is not None: world.reject(cached["candidate"],cached["error"])
                    else: world.add(cached["candidate"],cached["view"])
                    db.execute("UPDATE shared_members SET added=1 WHERE world=? AND id=?",pending)
            last_capital_batch=-1

            def advance_shared(through, terminal=False):
                nonlocal last_capital_batch
                # Decision and execution events interleave. No completed future
                # cycle or another capital/latency world's result informs an
                # earlier decision. The no-release plans above are diagnostics.
                while True:
                    found=db.execute("SELECT available FROM resource_requests WHERE available>? AND available<? "
                                     "ORDER BY available LIMIT 1",(last_capital_batch,through)).fetchone()
                    if found is None: break
                    when=found[0];batch=[];candidates={}
                    for identifier,body in db.execute("SELECT id,body FROM resource_requests WHERE available=? ORDER BY id",(when,)):
                        batch.append(json.loads(body))
                        require(len(batch)<=64,"resource_batch_capacity")
                        candidates[identifier]=json.loads(db.execute("SELECT body FROM candidates WHERE id=?",(identifier,)).fetchone()[0])
                    for world_id,world in sorted(worlds.items()):
                        world.advance(Fraction(when,1000000))
                        world.before_event(Fraction(when,1000000))
                        capital,arm=world_terms[world_id]
                        identifier=sha([study_id,world_id,when])
                        if world.tainted is not None:
                            plan={**SAFETY,"selected_ids":[],"rejected":{r["request_id"]:"CENSORED_WORLD" for r in batch},
                                  "expected_pnl":None,"reason":world.tainted}
                        else:
                            capacities={"PUSD":fstr(capital)}
                            for request in batch:
                                candidate=candidates[request["request_id"]]
                                side="asks" if candidate["result"]["direction"]=="BUY" else "bids"
                                for token,book in candidate["decision_books"].items():
                                    for price,size in book[side]:
                                        key="depth:"+sha([candidate["observer_session_id"],token,side,fstr(frac(price))])
                                        if key not in request["depth_capacities"]: continue
                                        # Previous fills consume displayed depth even after funding
                                        # is released. Pending depth reservations stay conservative.
                                        value=fstr(max(Fraction(0),frac(size)-world.depletion[(token,side,None,price)]))
                                        require(key not in capacities or capacities[key]==value,"conflicting_decision_depth_capacity")
                                        capacities[key]=value
                            snapshot={**SAFETY,"schema":"polymarket_v7_exact_arb_shadow_capacity_v1","model_sha":model,
                                "capacity_basis":"TOTAL_BEFORE_RESEARCH_HOLDS_NOT_EXECUTION_AUTHORITY","available_ns":when,
                                "expires_ns":min(r["valid_until_ns"] for r in batch),"capacities":capacities}
                            plan=capital_journals[world_id].admit(identifier,batch,snapshot,when)
                        plan.update(study_id=study_id,world_id=world_id,hypothetical_paper_capital=fstr(capital),
                                    arm_id=arm["arm_id"],available_ns=when,policy=ShadowCapitalJournal.POLICY)
                        db.execute("INSERT INTO capital_plans VALUES(?,?)",(identifier,canonical(plan)))
                        for selected in plan["selected_ids"]:
                            db.execute("INSERT INTO shared_members(world,id,available) VALUES(?,?,?)",(world_id,selected,when))
                    add_shared_members(when+1)
                    last_capital_batch=when
                    guard()
                for world in worlds.values():
                    if terminal: world.finish()
                    else: world.advance(Fraction(through,1000000))
                db.commit()

            def evaluate(through, terminal=False):
                # Strictly greater availability gives each queried instant a
                # following frame, exposing loss intervals and same-time resets.
                query="SELECT id,arm FROM tasks WHERE done=0"
                params=()
                if not terminal: query+=" AND finish<?";params=(through,)
                query+=" ORDER BY finish,id,arm LIMIT 1"
                while True:
                    pending=db.execute(query,params).fetchone()
                    if pending is None: break
                    identifier,index=pending
                    candidate=json.loads(db.execute("SELECT body FROM candidates WHERE id=?",(identifier,)).fetchone()[0])
                    arm=arms[index]
                    try:
                        view=control.view(history.for_candidate(candidate),candidate)
                        if terminal:
                            raise GraphError("native_arrival_no_strict_following_frame")
                        cycle=simulate(candidate,view,arm["mode"],frac(arm["delay_ms"]),frac(arm["skew_ms"]),
                                       unwind_delay_ms=frac(arm["unwind_delay_ms"]),order_type=arm["order_type"],
                                       venue_delay_ms_by_token=arm["venue_delay_ms_by_token"],ack_delay_ms=arm["ack_delay_ms"])
                    except GraphError as error:
                        cycle={**SAFETY,"schema":"polymarket_v7_native_exact_arb_unavailable_scenario_v1",
                            "state":"CENSORED","reason":str(error),"opportunity_id":identifier,
                            "model_sha":model,"venue_execution_verified":False,"net_locked_pnl":None,
                            "realized_counterfactual_pnl":None,"arrival_evidence":history.evidence_identity()}
                    cycle["arm_id"]=arm["arm_id"]
                    cycle["research_scenario_id"]=sha([study_id,identifier,arm])
                    db.execute("INSERT INTO cycles VALUES(?,?,?)",(cycle["research_scenario_id"],index,canonical(cycle)))
                    db.execute("UPDATE tasks SET done=1 WHERE id=? AND arm=?",(identifier,index))
                    guard()
                db.commit()

            for sequence in history.advance():
                advance_shared(history.available)
                evaluate(history.available)
            # Candidate decisions at/beyond the final watermark cannot submit
            # known orders. They still get explicit censored shared evidence.
            for world_id,identifier in db.execute("SELECT world,id FROM shared_members WHERE added=0 ORDER BY world,id"):
                candidate=json.loads(db.execute("SELECT body FROM candidates WHERE id=?",(identifier,)).fetchone()[0])
                worlds[world_id].reject(candidate,"observation_watermark")
            advance_shared(history.available,True)
            require(not pending_keys and not pending_candidates and pending_bytes==0,"shared_pending_leak")
            evaluate(history.available,True)
            totals=[]
            for index,arm in enumerate(arms):
                states=Counter();reasons=Counter();positive=0;partial=0
                for (body,) in db.execute("SELECT body FROM cycles WHERE arm=? ORDER BY id",(index,)):
                    cycle=json.loads(body);states[cycle["state"]]+=1
                    if cycle.get("reason"): reasons[cycle["reason"]]+=1
                    partial+=cycle.get("partial_fill") is True
                    positive+=cycle.get("net_locked_pnl") is not None and frac(cycle["net_locked_pnl"])>0
                totals.append({**arm,"states":dict(states),"reasons":dict(reasons),"scenario_count":sum(states.values()),
                    "partial_fill_scenarios":partial,"conditional_positive_locked_pnl_scenarios":positive,
                    "portfolio_net_pnl":None,"opportunities_per_hour":None})
            require(code==source_hashes(),"runner_source_changed")
            staging=root/"report";staging.mkdir()
            files={"scenarios.jsonl":write_rows(db,"SELECT body FROM cycles ORDER BY id",staging/"scenarios.jsonl",guard),
                   "admissions.jsonl":write_rows(db,"SELECT body FROM admissions ORDER BY id",staging/"admissions.jsonl",guard),
                   "episodes.jsonl":write_rows(evidence.db,"SELECT payload FROM episodes ORDER BY id",staging/"episodes.jsonl",guard),
                   "resource_plans.jsonl":write_rows(db,"SELECT body FROM resource_plans ORDER BY id",staging/"resource_plans.jsonl",guard),
                   "shared_scenarios.jsonl":write_rows(db,"SELECT body FROM shared_cycles ORDER BY id",staging/"shared_scenarios.jsonl",guard)}
            for name,table in (("capital_plans.jsonl","capital_plans"),("capital_transitions.jsonl","capital_transitions")):
                files[name]=write_rows(db,"SELECT body FROM "+table+" ORDER BY id",staging/name,guard)
            hourly_digest=hashlib.sha256();hourly_count=0
            with (staging/"hourly.jsonl").open("wb") as stream:
                for row in hourly.rows(study_id,model,session,manifest_hash):
                    payload=(canonical(row)+"\n").encode();stream.write(payload);hourly_digest.update(payload)
                    hourly_count+=1;stream.flush();guard()
            files["hourly.jsonl"]=hourly_digest.hexdigest()
            files["control_events.jsonl"]=write_rows(db,"SELECT wire FROM control_events ORDER BY seq",staging/"control_events.jsonl",guard)
            report={**SAFETY,"schema":"polymarket_v7_native_exact_arb_study_v1","model_sha":model,
                "study_id":study_id,"normalized_full_evidence_sha256":full_hash.hexdigest(),
                "observer_session_id":session,"session_manifest_sha256":manifest_hash,"replay_receipt":archive.receipt,
                "runner_source_sha256":code,"diagnostics":diagnostics,"episode_admission":dict(counts),
                "latency_attribution":latency_attribution,
                "hourly_attribution":{"artifact":"hourly.jsonl","windows":hourly_count,
                    "window_ns":3600000000000,"utc_alignment_verified":False,
                    "publication_mode":"COMPLETE_VALIDATED_STUDY_NOT_LIVE_HOURLY_TIMER",
                    "maximum_windows":hourly.maximum_windows},
                "runtime_health_snapshot":health,
                "control_history":control.receipt(),
                "arms":totals,"artifact_sha256":files,"config":config,
                "shared_execution":{"worlds":[{**world.receipt(),"hypothetical_paper_capital":fstr(world_terms[key][0]),
                    "capital_lifecycle":capital_journals[key].capital_receipt(),
                    "arm_id":world_terms[key][1]["arm_id"]} for key,world in sorted(worlds.items())],
                    "peak_pending_jobs":peak_pending,"portfolio_net_pnl":None},
                "execution_evidence_scope":"INDEPENDENT_ARMS_AND_SHARED_PLANNED_PORTFOLIOS_NOT_VERIFIED_ECONOMICS",
                "data_quality":{"native_replay_validated":True,"missing_feed_frames":archive.receipt["missing_frames"],
                    "capital_requests_without_following_replay_frame":db.execute(
                        "SELECT count(*) FROM resource_requests WHERE available>=?",(history.available,)).fetchone()[0],
                    "control_prefix_validated":control.available,
                    "invalid_feed_frames":archive.receipt["invalid_frames"],"tail_completeness_verified":False},
                "economic_evidence":{"net_pnl":None,"capital_efficiency":None,"coverage":None,"research_decision":None},
                "limitations":["One recorded session, not an exchange-wide or London health attestation.",
                    "Producer tail completeness, venue execution, inventory and resource admission are unverified.",
                    "Independent scenarios do not share resources; each shared world interleaves decision-time resource allocation and modeled execution results causally. No-release resource plans are a separate baseline.",
                    "Conditional locked PnL is not realized PnL; no PnL sum or rate is inferred.",
                    "Missing venue-hold profiles are explicitly unverified zero-hold upper bounds. Supplied token holds and response delays are frozen assumptions, not session-bound venue attestations.",
                    "Source hashes identify analysis code, not an official deployed model artifact.",
                    "This runner joins native diagnostic decisions; it does not independently rerun graph decisions on every WS frame.",
                    "Only fully accounted modeled order outcomes release excess funding. Reserve remains encumbered; acquired tokens are not redeemed. Censored/residual/transform outcomes keep their holds. Public-L2 fee/matching/ACK and settlement remain unverified; this is not an economic capacity curve.",
                    "History eviction and absent full evidence censor scenarios, never create zero profits."]}
            report["limitations"].append("Hourly windows use session monotonic clocks, not verified UTC or uptime. Counters describe recorded rows; sequence losses are attributed when detected, not when lost. Episode starts carry across hours. Results are bucketed by modeled availability, not decision cohorts; alternative worlds are never pooled. Blank windows do not establish zero market activity. No live hourly scheduler or multi-session economic aggregation is certified.")
            report["limitations"].append("Missing control journals censor execution. A checkpoint bounds local metadata continuity, not venue semantics or unknown producer tail.")
            report["limitations"].append("REQUIRE_RECORDED_SELECTION_TERMS verifies archived public projections and their exact native admission, not atomic source snapshots, fee fragmentation or guaranteed match-time immutability. UNVERIFIED_SCENARIOS is diagnostic only.")
            for name,payload in health_artifacts.items():
                (staging/name).write_bytes(payload)
                files[name]=hashlib.sha256(payload).hexdigest()
            for name,document in render_sota(report).items():
                payload=document.encode("utf-8")
                (staging/name).write_bytes(payload)
                files[name]=hashlib.sha256(payload).hexdigest()
            require(code==source_hashes(),"runner_source_changed")
            wire=canonical(report)+"\n";(staging/"report.json").write_text(wire)
            guard()
            destination=output/hashlib.sha256(wire.encode()).hexdigest()
            if destination.exists():
                require((destination/"report.json").read_bytes()==wire.encode(),"existing_report_corrupt")
                for name,digest in files.items():
                    require(file_digest(destination/name)==digest,"existing_artifact_corrupt")
            else: staging.rename(destination)
            return destination/"report.json"
        finally:
            for journal in capital_journals.values(): journal.close()
            if history is not None: history.close()
            if db is not None: db.close()
            if evidence is not None: evidence.close()
            archive.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("session-manifest","replay-output","bundles","output"):
        parser.add_argument("--"+name,required=True,type=Path)
    for name in ("observations","full-evidence"):
        parser.add_argument("--"+name,required=True,nargs="+",type=Path)
    parser.add_argument("--model-sha",required=True)
    parser.add_argument("--arms",type=Path,help="Frozen JSON array; default parallel 1/2/5/10/25/50/100ms")
    parser.add_argument("--maximum-rows",type=int,default=20000000)
    parser.add_argument("--maximum-bytes",type=int,default=4*1024**3)
    parser.add_argument("--maximum-working-bytes",type=int,default=8*1024**3)
    parser.add_argument("--capital-budgets",nargs="+",default=["1000","10000","100000"])
    parser.add_argument("--runtime-health",type=Path,help="Optional canonical read-only health receipt; requires target and explicit as-of")
    parser.add_argument("--runtime-target",type=Path,help="Canonical runtime target manifest; no historical instance fallback")
    parser.add_argument("--report-as-of-ms",type=int,help="Frozen health assessment time, not an inferred current-runtime attestation")
    parser.add_argument("--control-events",nargs="+",type=Path,help="Native control journal prefix ending in CHECKPOINT; missing evidence censors all execution scenarios")
    parser.add_argument("--require-venue-terms",action="store_true",help="Require public terms from each exact admitted selection and its sibling native_venue_terms archive; missing data censors execution")
    args=parser.parse_args()
    arms=None
    if args.arms:
        require(args.arms.stat().st_size<=65536,"arms_size")
        arms=json.loads(args.arms.read_text())
    print(run(manifest=args.session_manifest,replay=args.replay_output,observations=args.observations,
              full_evidence=args.full_evidence,bundles=args.bundles,model=args.model_sha,output=args.output,
              arms=arms,maximum_rows=args.maximum_rows,maximum_bytes=args.maximum_bytes,
              maximum_working_bytes=args.maximum_working_bytes,capital_budgets=args.capital_budgets,
              runtime_health=args.runtime_health,runtime_target=args.runtime_target,report_as_of_ms=args.report_as_of_ms,
              control_events=args.control_events,require_venue_terms=args.require_venue_terms))


if __name__=="__main__": main()
