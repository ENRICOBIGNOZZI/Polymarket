"""Event-ordered, shared-depth counterfactual venue. No execution authority.

Every portfolio/latency arm has its own queue and liquidity debits. Book versions
do not replenish consumed depth. Only observed absence in a complete valid book
clears a price-level debit. This is a conservative public-L2 counterfactual, not
an exchange matching-engine or queue-priority attestation.
"""
from collections import defaultdict
from fractions import Fraction
import heapq
import json

from v7_unified_exact_arb_graph import SAFETY, GraphError, frac, fstr, sha
from v7_unified_exact_arb_graph_execution_shadow import execution_steps


class SharedDepthDebit:
    def __init__(self, maximum_levels=131072):
        self.amounts={};self.by_side=defaultdict(set);self.maximum_levels=maximum_levels
        self.observed_zero_resets=0

    @staticmethod
    def key(raw):
        token,side,version,price=raw
        if side not in {"asks","bids"} or not isinstance(token,str) or not token:
            raise GraphError("shared_depth_key")
        return token,side,frac(price)

    def __getitem__(self, key):
        return self.amounts.get(self.key(key),Fraction(0))

    def __setitem__(self, key, amount):
        key=self.key(key);amount=frac(amount)
        if amount<0: raise GraphError("negative_shared_depletion")
        if key not in self.amounts and len(self.amounts)>=self.maximum_levels:
            raise GraphError("shared_depth_capacity")
        self.amounts[key]=amount;self.by_side[key[:2]].add(key[2])

    def observe(self, token, asks, bids, *, valid, continuous, ask_truncated, bid_truncated):
        if not valid or not continuous: return
        if asks and bids and max(frac(p) for p,q in bids)>=min(frac(p) for p,q in asks):
            return  # crossed/locked state cannot attest disappearance/replenishment
        for side,raw,truncated in (("asks",asks,ask_truncated),("bids",bids,bid_truncated)):
            if truncated: continue
            visible={frac(price) for price,size in raw if frac(size)>0}
            key=(token,side)
            for price in tuple(self.by_side.get(key,())):
                if price not in visible:
                    self.amounts.pop((token,side,price),None)
                    self.by_side[key].remove(price);self.observed_zero_resets+=1


class NativeLiquidityFeed:
    """Advance debit lifecycle with original frame availability, never venue time."""
    def __init__(self, archive, debit):
        self.archive=archive;self.debit=debit;self.sequence=0;self.now=Fraction(0)

    def advance(self, timestamp):
        timestamp=frac(timestamp)
        if timestamp<self.now: raise GraphError("shared_liquidity_clock_reversal")
        self.now=timestamp
        ns=timestamp*1000000
        if not self.debit.amounts:
            row=self.archive.db.execute("SELECT seq FROM replay WHERE available<=? ORDER BY available DESC,seq DESC LIMIT 1",
                                        (ns.numerator//ns.denominator,)).fetchone()
            if row: self.sequence=max(self.sequence,row[0])
            return
        for sequence,wire in self.archive.db.execute(
            "SELECT seq,wire FROM replay WHERE seq>? AND available<=? ORDER BY seq",
            (self.sequence,ns.numerator//ns.denominator)):
            frame=json.loads(wire)
            if frame["source_frame_valid"]:
                for book in frame["books"]:
                    self.debit.observe(book["token_id"],
                        [(Fraction(p,10000),Fraction(q,1000000)) for p,q in book["asks_e4_microshares"]],
                        [(Fraction(p,10000),Fraction(q,1000000)) for p,q in book["bids_e4_microshares"]],
                        valid=book["valid"],continuous=book["lineage_continuous"],
                        ask_truncated=book["ask_truncated"],bid_truncated=book["bid_truncated"])
            self.sequence=sequence


class SharedExecutionQueue:
    def __init__(self, arm, world_id, on_result, before_event=None, maximum_pending=4096):
        self.arm=dict(arm);self.world_id=world_id;self.on_result=on_result
        self.before_event=before_event;self.maximum_pending=maximum_pending
        self.depletion=SharedDepthDebit();self.heap=[];self.jobs={};self.origins={};self.seen=set()
        self.now=Fraction(0);self.processed=0;self.tainted=None;self.trace=""

    def _schedule(self, identifier, event):
        due=frac(event["timestamp_ms"])
        if due<self.now: raise GraphError("shared_execution_time_reversal")
        heapq.heappush(self.heap,(due,identifier,event["leg_index"],event["kind"],event))

    def _finish(self, identifier, result):
        self.jobs.pop(identifier,None)
        failure_time=max(self.now,self.origins.pop(identifier))
        result.update(shared_world_id=self.world_id,
            shared_execution_id=sha([self.world_id,identifier]),
            shared_depth_policy="PERSIST_DEBIT_UNTIL_OBSERVED_ZERO_IN_VALID_FULL_BOOK",
            same_time_ordering="CANONICAL_OPPORTUNITY_ID_THEN_LEG_NOT_VERIFIED_VENUE_PRIORITY",
            shared_event_trace_sha256=self.trace,
            resource_release_verified=False)
        if result["state"]=="CENSORED" and (self.tainted is None or failure_time<frac(self.tainted["timestamp_ms"])):
            self.tainted={"timestamp_ms":fstr(failure_time),"opportunity_id":identifier,"reason":result.get("reason")}
        self.on_result(result)

    def _resume(self, identifier, error=None):
        steps=self.jobs[identifier]
        try:
            event=steps.throw(GraphError(error)) if error else next(steps)
        except StopIteration as done:
            self._finish(identifier,done.value)
        else: self._schedule(identifier,event)

    def add(self, opportunity, history):
        identifier=opportunity["opportunity_id"]
        if identifier in self.seen: raise GraphError("duplicate_shared_opportunity")
        if len(self.seen)>=100000: raise GraphError("shared_episode_capacity")
        if len(self.jobs)>=self.maximum_pending: raise GraphError("shared_pending_capacity")
        if frac(opportunity["timestamp_ms"])<self.now: raise GraphError("late_shared_admission")
        self.seen.add(identifier)
        steps=execution_steps(opportunity,history,self.arm["mode"],frac(self.arm["delay_ms"]),frac(self.arm["skew_ms"]),
            unwind_delay_ms=frac(self.arm["unwind_delay_ms"]),order_type=self.arm["order_type"],depletion=self.depletion,
            venue_delay_ms_by_token=self.arm.get("venue_delay_ms_by_token"),ack_delay_ms=self.arm.get("ack_delay_ms",0))
        self.jobs[identifier]=steps
        self.origins[identifier]=frac(opportunity["timestamp_ms"])
        self._resume(identifier)

    def reject(self, opportunity, reason):
        identifier=opportunity["opportunity_id"]
        if identifier in self.seen or len(self.seen)>=100000: raise GraphError("shared_episode_identity_or_capacity")
        self.seen.add(identifier);self.origins[identifier]=frac(opportunity["timestamp_ms"])
        self._finish(identifier,{**SAFETY,"schema":"polymarket_v7_shared_exact_arb_unavailable_v1",
            "state":"CENSORED","reason":reason,"opportunity_id":identifier,"model_sha":opportunity["model_sha"],
            "venue_execution_verified":False,"net_locked_pnl":None,"realized_counterfactual_pnl":None,
            "known_entry_fills":{},"residual_exposure_verified":False})

    def advance(self, through):
        through=frac(through)
        if through<self.now: raise GraphError("shared_watermark_reversal")
        while self.heap and self.heap[0][0]<through:
            due,identifier,index,kind,event=heapq.heappop(self.heap)
            self.now=due
            if self.tainted is not None and due>=frac(self.tainted["timestamp_ms"]):
                self._resume(identifier,"shared_venue_uncertain_after_censored_order")
                continue
            if self.before_event is not None: self.before_event(due)
            self.processed+=1
            self.trace=sha([self.trace,fstr(due),identifier,index,kind])
            self._resume(identifier)

    def finish(self):
        # Unknown remaining arrivals may consume liquidity; do not complete
        # other baskets on the assumption those orders certainly did not fill.
        while self.heap:
            due,identifier,index,kind,event=heapq.heappop(self.heap)
            self.now=max(self.now,due)
            self._resume(identifier,"observation_watermark")
        if self.jobs: raise GraphError("unfinished_shared_execution_jobs")

    def receipt(self):
        return {"shared_world_id":self.world_id,"events_processed":self.processed,
            "event_trace_sha256":self.trace,"censored_world":self.tainted,
            "observed_zero_debit_resets":self.depletion.observed_zero_resets,
            "remaining_depth_debit_levels":len(self.depletion.amounts),"pending_jobs":len(self.jobs),
            "portfolio_net_pnl":None,"venue_execution_verified":False,"resource_release_verified":False}
