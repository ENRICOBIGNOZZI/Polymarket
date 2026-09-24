"""Bounded, offline hourly attribution of a validated native PAPER study.

Windows use the recorded session's monotonic clock, NOT assumed UTC or uptime.
The caller supplies only deduplicated, validated diagnostic counter deltas.
Episode state is maintained by the existing whole-session reducer, never reset
at hour boundaries. Hypothetical worlds remain separate and unverified.
"""
from collections import Counter
from fractions import Fraction
import json

from v7_exact_arb_native_evidence import SAFETY, canonical, integer, QUANTILES
from v7_exact_arb_sota_report import STAGES as LATENCY_STAGES
from v7_exact_arb_sota_report import QUANTILES as LATENCY_QUANTILES


HOUR_NS=3600*1000000000
SCHEMA="polymarket_v7_native_exact_arb_hourly_v1"
READINESS=("evaluations","books_ready","lineage_ready","fees_ready","freshness_ready",
           "leg_skew_ready","depth_complete","accepted_evaluations","sizing_unknown_evaluations")
COUNTERS=READINESS+tuple(stage+suffix for stage in ("raw","after_fee","after_reserve","pre_allocation")
                       for suffix in ("_positive_evaluations","_positive_segments","_observed_starts"))


class HourlyStudy:
    def __init__(self,db,archive,maximum_windows=4096):
        if type(maximum_windows) is not int or not 0<maximum_windows<=4096:
            raise ValueError("hourly_window_budget")
        self.db,self.archive,self.maximum_windows=db,archive,maximum_windows
        self.first=self.last=None
        self.last_observation_end=0
        def rational_order(a,b):
            left,right=Fraction(a),Fraction(b)
            return (left>right)-(left<right)
        self.db.create_collation("EXACT_RATIONAL",rational_order)
        self.db.executescript("""
          CREATE TABLE hourly_observations(seq INTEGER PRIMARY KEY, hour INTEGER, family TEXT,
              relation TEXT, generation TEXT, available INTEGER, counters TEXT, missing_detected INTEGER);
          CREATE INDEX hourly_observations_window ON hourly_observations(hour,family);
          CREATE TABLE hourly_distances(hour INTEGER,family TEXT,stage TEXT,
              pusd TEXT COLLATE EXACT_RATIONAL,ticks TEXT COLLATE EXACT_RATIONAL,bps TEXT COLLATE EXACT_RATIONAL);
          CREATE TABLE hourly_frames(hour INTEGER,seq INTEGER PRIMARY KEY,available INTEGER,
              receive INTEGER,wall INTEGER,valid INTEGER,missing_detected INTEGER,wall_backward INTEGER);
          CREATE INDEX hourly_frames_window ON hourly_frames(hour);
        """)
        previous=0;previous_wall=None
        for sequence,available,wire in archive.db.execute("SELECT seq,available,wire FROM replay ORDER BY seq"):
            row=json.loads(wire);hour=self._window(available)
            wall=row.get("receive_wall_ms")
            backwards=int(wall<previous_wall) if wall is not None and previous_wall is not None else None
            self.db.execute("INSERT INTO hourly_frames VALUES(?,?,?,?,?,?,?,?)",
                (hour,sequence,available,row["receive_monotonic_ns"],row.get("receive_wall_ms"),
                 int(row["source_frame_valid"]),sequence-previous-1,backwards))
            previous=sequence;previous_wall=wall

    def _window(self,stamp):
        if type(stamp) is not int or not 0<stamp<2**63: raise ValueError("hourly_clock")
        self.first=stamp if self.first is None else min(self.first,stamp)
        self.last=stamp if self.last is None else max(self.last,stamp)
        if self.last//HOUR_NS-self.first//HOUR_NS+1>self.maximum_windows:
            raise ValueError("hourly_window_budget")
        return stamp//HOUR_NS

    def ingest(self,row,family,counters,relation,missing_detected):
        """Called only after Evidence.ingest and LatencyEvidence validation."""
        stamp=integer(row,"decision_end_ns",1)
        if stamp<self.last_observation_end: raise ValueError("hourly_emission_time_reversal")
        self.last_observation_end=stamp
        hour=self._window(stamp)
        self.db.execute("INSERT INTO hourly_observations VALUES(?,?,?,?,?,?,?,?)",
            (row["observation_sequence"],hour,family,relation["economic_identity"],row["graph_generation"],
             row["decision_end_ns"],canonical(dict(counters)),missing_detected))
        near=row["near_arbitrage"]
        if not near["valid"]: return
        tick,guarantee=near["actionable_tick_nano"],near["guarantee_nano"]
        for stage in ("raw","after_fee","after_reserve"):
            distance=near[stage+"_distance_nano"]
            self.db.execute("INSERT INTO hourly_distances VALUES(?,?,?,?,?,?)",
                (hour,family,stage,str(Fraction(distance,1000000000)),
                 str(Fraction(distance,tick)),str(Fraction(distance*10000,guarantee))))

    def _quantiles(self,table,column,where,args,count,probabilities=None):
        # Private SQL names only. Exact fractions are sorted by rational value,
        # not lexical order or floating conversion. Empty samples stay unknown.
        result={}
        probabilities=probabilities or {str(q):Fraction(str(q)) for q in QUANTILES}
        for label,q in probabilities.items():
            index=q*(count-1)
            result[label]=self.db.execute("SELECT "+column+" FROM "+table+" WHERE "+where+
                " ORDER BY "+column+" LIMIT 1 OFFSET ?",(*args,index.numerator//index.denominator)).fetchone()[0] if count else None
        return result

    def _family(self,hour,family):
        totals=Counter({k:0 for k in COUNTERS})
        for (body,) in self.db.execute("SELECT counters FROM hourly_observations WHERE hour=? AND family=?",(hour,family)):
            totals.update(json.loads(body))
        # pre-allocation has accepted evaluations, not an independently measured
        # top-of-book positive stage. Do not invent that unsupported counter.
        totals.pop("pre_allocation_positive_evaluations",None)
        metrics={}
        for stage in ("raw","after_fee","after_reserve"):
            where,args="hour=? AND family=? AND stage=?",(hour,family,stage)
            count=self.db.execute("SELECT count(*) FROM hourly_distances WHERE "+where,args).fetchone()[0]
            within={str(t):0 for t in (Fraction(1,4),Fraction(1,2),Fraction(1),Fraction(2),Fraction(5))}
            for (ticks,) in self.db.execute("SELECT ticks FROM hourly_distances WHERE "+where,args):
                for t in within: within[t]+=abs(Fraction(ticks))<=Fraction(t)
            metrics[stage]={"observations":count,"quantiles":{
                unit:self._quantiles("hourly_distances",unit,where,args,count) for unit in ("pusd","ticks","bps")},
                "fraction_within_ticks":{t:str(Fraction(n,count)) if count else None for t,n in within.items()}}
        relations=self.db.execute("SELECT count(DISTINCT relation) FROM hourly_observations WHERE hour=? AND family=?",(hour,family)).fetchone()[0]
        return {"evaluation_funnel":dict(totals),"relations_observed":relations,"near_arbitrage":metrics}

    def rows(self,study_id,model,session,manifest_hash):
        for unit in ("pusd","ticks","bps"):
            self.db.execute("CREATE INDEX hourly_distance_"+unit+" ON hourly_distances(hour,family,stage,"+unit+")")
        if self.first is None: return
        # Outcomes use causal receipt availability, not opportunity start time.
        # This keeps a fill arriving in a later hour out of the earlier receipt.
        self.db.execute("CREATE TABLE hourly_capital(hour INTEGER,world TEXT,body TEXT)")
        self.db.execute("CREATE INDEX hourly_capital_window ON hourly_capital(hour,world)")
        for (body,) in self.db.execute("SELECT body FROM capital_transitions ORDER BY id"):
            row=json.loads(body);stamp=integer(row,"available_ns",1)
            if stamp>self.last: raise ValueError("hourly_future_capital_result")
            if stamp<self.first: raise ValueError("hourly_pre_capture_capital_result")
            self.db.execute("INSERT INTO hourly_capital VALUES(?,?,?)",(stamp//HOUR_NS,row["world_id"],body))
        for hour in range(self.first//HOUR_NS,self.last//HOUR_NS+1):
            start,end=hour*HOUR_NS,(hour+1)*HOUR_NS
            frame_count,invalid,missing,wall_count=self.db.execute(
                "SELECT count(*),sum(1-valid),sum(missing_detected),count(wall) FROM hourly_frames WHERE hour=?",(hour,)).fetchone()
            count,missing_observations=self.db.execute("SELECT count(*),sum(missing_detected) FROM hourly_observations WHERE hour=?",(hour,)).fetchone()
            family_names=self.db.execute("SELECT DISTINCT family FROM hourly_observations WHERE hour=? ORDER BY family LIMIT 129",(hour,)).fetchall()
            if len(family_names)>128: raise ValueError("hourly_family_budget")
            families={f:self._family(hour,f) for (f,) in family_names}
            latency={}
            for stage in LATENCY_STAGES:
                where="seq IN (SELECT seq FROM hourly_observations WHERE hour=?)"
                samples=self.db.execute("SELECT count(*) FROM latency WHERE "+where,(hour,)).fetchone()[0]
                latency[stage]={"samples":samples,"quantiles_ns":self._quantiles("latency",stage,where,(hour,),samples,LATENCY_QUANTILES)}
            worlds={}
            for world,body in self.db.execute("SELECT world,body FROM hourly_capital WHERE hour=? ORDER BY world,body",(hour,)):
                result=json.loads(body)
                subtotal=worlds.setdefault(world,{"closed_order_groups":0,"unresolved_order_groups":0,
                    "states":Counter(),"modeled_flat_net_pnl":Fraction(0),"flat_pnl_groups":0,
                    "reservation_pusd_seconds_closed_groups":Fraction(0),"portfolio_net_pnl":None,
                    "venue_execution_verified":False,"settlement_release_verified":False})
                subtotal["states"][result["execution_state"]]+=1
                subtotal["closed_order_groups"]+=result["modeled_orders_closed"]
                subtotal["unresolved_order_groups"]+=not result["modeled_orders_closed"]
                if result["modeled_flat_net_pnl"] is not None:
                    subtotal["modeled_flat_net_pnl"]+=Fraction(result["modeled_flat_net_pnl"]);subtotal["flat_pnl_groups"]+=1
                if result["reservation_pusd_seconds"] is not None:
                    subtotal["reservation_pusd_seconds_closed_groups"]+=Fraction(result["reservation_pusd_seconds"])
            for values in worlds.values():
                values["states"]=dict(values["states"])
                values["modeled_flat_net_pnl"]=str(values["modeled_flat_net_pnl"]) if values["flat_pnl_groups"] else None
                values["reservation_pusd_seconds_closed_groups"]=str(values["reservation_pusd_seconds_closed_groups"]) if values["closed_order_groups"] else None
            wall_range=self.db.execute("SELECT min(wall),max(wall) FROM hourly_frames WHERE hour=?",(hour,)).fetchone()
            wall_backwards=self.db.execute("SELECT sum(wall_backward) FROM hourly_frames WHERE hour=?",(hour,)).fetchone()[0]
            yield {**SAFETY,"schema":SCHEMA,"study_id":study_id,"model_sha":model,
                "observer_session_id":session,"session_manifest_sha256":manifest_hash,
                "window_start_monotonic_ns":start,"window_end_monotonic_ns":end,
                "hour_index":hour,"recorded_as_of_monotonic_ns":self.last,
                "time_basis":"SESSION_HOST_MONOTONIC_HOURS_NOT_UTC_OR_OBSERVED_UPTIME",
                "recorded_clock_reached_window_end":self.last>=end,
                "data_state":"RECORDED_ROWS" if count or frame_count else "NO_RECORDED_ROWS_NOT_OBSERVED_ZERO_ACTIVITY",
                "engineering_evidence":{"recorded_frames":frame_count,"invalid_frames":invalid if frame_count else None,
                    "missing_frames_detected":missing if frame_count else None,
                    "recorded_evaluations":count,"missing_observations_detected":missing_observations if count else None,
                    "missing_raw_frame_observations":count-latency[LATENCY_STAGES[0]]["samples"],
                    "native_observations_dropped":None,"producer_tail_completeness_verified":False},
                "reported_host_wall_clock":{"frames_with_wall_time":wall_count,"minimum_ms":wall_range[0],
                    "maximum_ms":wall_range[1],"backward_transitions_detected":wall_backwards,"utc_alignment_verified":False},
                "families":families,"latency_attribution":latency,"shared_result_worlds":worlds,
                "runtime_health":None,"causal_exchange_coverage":None,
                "economic_evidence":{"fully_filled_opportunities_per_hour":None,"conservative_net_pnl":None,
                    "capital_efficiency":None,"research_decision":None},
                "scope":"DIAGNOSTICS_BY_EMISSION_TIME_MODEL_RESULTS_BY_AVAILABILITY_NO_RATE_OR_PROFIT_INFERENCE"}
