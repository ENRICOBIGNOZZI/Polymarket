"""Shared causal evidence and quantity-resource contracts for graph shadows."""
from __future__ import annotations
from bisect import bisect_right
from collections import defaultdict
from fractions import Fraction
import json
import os
from v7_unified_exact_arb_graph import GraphError, frac


def decode_snapshot(row, model_sha):
    if (row.get("schema") != "polymarket_v7_pure_arb_deep_book_snapshot_v1"
        or row.get("model_sha") != model_sha or row.get("paper_only") is not True
        or row.get("authenticated_execution") is not False or row.get("real_order_submission") is not False
        or row.get("execution_authority") != "ZERO_AUTHORITY_RESEARCH_ONLY"):
        raise GraphError("snapshot_identity")
    now = int(row["receive_wall_ms"])
    session, epoch = str(row.get("observer_session_id") or ""), int(row.get("connection_epoch") or 0)
    if now <= 0 or not session or epoch <= 0: raise GraphError("snapshot_lineage_identity")
    books = {}
    for side in ("yes", "no"):
        token = str(row.get(side+"_token") or "")
        if not token or token in books: raise GraphError("snapshot_token_identity")
        stamp = int(row.get(side+"_receive_wall_ms") or 0)
        if stamp <= 0 or stamp > now: raise GraphError("snapshot_clock")
        books[token] = {"timestamp_ms":stamp, "observation_ms":now,
            "lineage_continuous":row.get(side+"_lineage_continuous") is True and row.get(side+"_valid") is True,
            "lineage_id":session+":"+str(epoch),
            "depth_truncated":row.get(side+"_ask_truncated") is not False or row.get(side+"_bid_truncated") is not False,
            "asks":[[x["price"],x["size"]] for x in row.get(side+"_ask_levels", [])],
            "bids":[[x["price"],x["size"]] for x in row.get(side+"_bid_levels", [])],
            "state_version":row.get(side+"_state_version")}
    return now, books


class CausalBooks:
    def __init__(self, maximum_snapshots=4096):
        self.history = defaultdict(list)
        self.times = defaultdict(list)
        self.maximum_snapshots = maximum_snapshots
        self.watermark = 0

    def ingest(self, now, books):
        if now < self.watermark: raise GraphError("timestamp_reversal")
        for token, book in books.items():
            self.history[token].append(book)
            self.times[token].append(now)
            if len(self.times[token]) > self.maximum_snapshots:
                del self.times[token][0]; del self.history[token][0]
        self.watermark = now

    def at(self, token, timestamp):
        # Selection is by local availability, never by exchange event time.
        index = bisect_right(self.times.get(token, []), timestamp)-1
        return self.history[token][index] if index >= 0 else None


class ResourceLedger:
    """Quantity reservations persist until verified release time, or indefinitely."""
    def __init__(self):
        self.reservations = {}

    def release(self, now):
        for key, row in list(self.reservations.items()):
            if row["release_ms"] is not None and row["release_ms"] <= now:
                del self.reservations[key]

    def used(self):
        total = defaultdict(Fraction)
        for row in self.reservations.values():
            for key, value in row["resources"].items(): total[key] += frac(value)
        return total

    def reserve(self, identity, resources, capacities, now, lock_ms):
        self.release(now)
        if identity in self.reservations: return False
        used = self.used()
        normalized = {key:frac(value) for key,value in resources.items()}
        if any(value < 0 or used[key]+value > frac(capacities.get(key, 0)) for key,value in normalized.items()):
            return False
        if lock_ms is not None and int(lock_ms) <= 0: return False
        self.reservations[identity] = {"release_ms":now+int(lock_ms) if lock_ms is not None else None,
                                     "resources":{k:str(v) for k,v in normalized.items()}}
        return True


class ReconstructedDepth:
    """Full anchors plus canonical observer deltas; gaps invalidate all claims.

    Anchors may be loaded ahead of the delta cursor. They are activated only
    when their local observation time is reached, and state_version prevents
    applying a delta twice to an anchor from the same decoded frame.
    """
    def __init__(self, model_sha):
        self.model_sha=model_sha; self.anchors=defaultdict(list); self.current={}
        self.sequence=0; self.lineage=None; self.gaps=0; self.watermark=0

    def anchor(self,row):
        now,books=decode_snapshot(row,self.model_sha)
        for token,book in books.items():
            self.anchors[token].append(book)

    def delta(self,row):
        if (row.get("schema")!="polymarket_v7_causal_book_observation_v1"
            or row.get("model_sha")!=self.model_sha or row.get("paper_only") is not True
            or row.get("authenticated_execution") is not False or row.get("real_order_submission") is not False):
            raise GraphError("delta_identity")
        now=int(row["receive_wall_ms"]);seq=int(row["observer_sequence"])
        lineage=str(row["observer_session_id"])+":"+str(row["connection_epoch"])
        if now<self.watermark:raise GraphError("delta_timestamp_reversal")
        invalidated={}
        if self.lineage is not None and (lineage!=self.lineage or seq!=self.sequence+1):
            self.gaps+=1
            invalidated={t:{**b,"lineage_continuous":False} for t,b in self.current.items()}
            self.current.clear()
            # Old anchors cannot heal a missing delta interval.
            for t in self.anchors:
                self.anchors[t]=[b for b in self.anchors[t] if b["observation_ms"]>=now]
        self.lineage=lineage;self.sequence=seq;self.watermark=now
        token=str(row["token_id"])
        ready=[b for b in self.anchors[token] if b["observation_ms"]<=now and b["lineage_id"]==lineage]
        if ready:
            self.current[token]=dict(ready[-1])
            self.anchors[token]=[b for b in self.anchors[token] if b["observation_ms"]>now]
        book=self.current.get(token)
        if book is None:
            return now,invalidated
        book=dict(book)
        old_version=int(book.get("state_version") or 0);version=int(row.get("state_version") or 0)
        if row.get("valid") is not True or row.get("lineage_continuous") is not True:
            book["lineage_continuous"]=False
        elif version>old_version:
            if row.get("deep_replay_reset") is True:
                book["lineage_continuous"]=False
            else:
                change=row.get("book_change")
                if isinstance(change,dict):
                    side={"BUY":"bids","SELL":"asks"}.get(change.get("side"))
                    if side is None:raise GraphError("delta_side")
                    price,size=frac(change["price"]),frac(change["size"])
                    if not 0<price<1 or size<0:raise GraphError("delta_level")
                    depth={frac(p):frac(q) for p,q in book[side]}
                    if size==0:depth.pop(price,None)
                    else:depth[price]=size
                    book[side]=[[str(p),str(q)] for p,q in sorted(depth.items(),reverse=side=="bids")]
                book["state_version"]=version
                receive=row.get("receive_monotonic_ns")
                book_receive=row.get("book_receive_monotonic_ns")
                if receive is not None and book_receive is not None:
                    age=int(receive)-int(book_receive)
                    if int(book_receive)<=0 or age<0:raise GraphError("delta_book_clock")
                    book["timestamp_ms"]=now-age//1000000
                elif isinstance(change,dict):
                    book["timestamp_ms"]=now
                # A trade/state-version update without depth evidence must not
                # make an old book fresh merely because it was observed again.
        book["observation_ms"]=now
        book["tick_size"]=row.get("tick_size")
        self.current[token]=book
        return now,{**invalidated,token:book}


def quantiles(values):
    values = sorted(float(frac(v)) for v in values)
    if not values: return {}
    return {str(p):values[int((len(values)-1)*p/100)] for p in (.1,1,5,10,25,50,75,90,99)} | {"min":values[0]}


class JsonlCursor:
    """Drain sealed segments and the old open inode before following rotation."""
    def __init__(self,path,segmented=False):
        self.path=path;self.segmented=segmented;self.handle=None;self.consumed=set()

    def poll(self,limit=10000):
        count=0
        while count<limit:
            if self.handle is None:
                candidates=(sorted(self.path.parent.glob("*.segment-*.jsonl")) if self.segmented else [])+[self.path]
                for candidate in candidates:
                    try:
                        handle=candidate.open("rb");stat=os.fstat(handle.fileno());identity=(stat.st_dev,stat.st_ino)
                    except OSError:continue
                    if identity in self.consumed:handle.close();continue
                    self.handle=handle;break
                if self.handle is None:return
            offset=self.handle.tell();raw=self.handle.readline()
            if raw.endswith(b"\n"):
                count+=1;yield json.loads(raw);continue
            self.handle.seek(offset)
            old=os.fstat(self.handle.fileno())
            try:current=self.path.stat()
            except OSError:return
            if (old.st_dev,old.st_ino)==(current.st_dev,current.st_ino):
                if current.st_size<offset:raise GraphError("tape_truncated")
                return
            if raw:raise GraphError("sealed_partial_record")
            self.consumed.add((old.st_dev,old.st_ino));self.handle.close();self.handle=None
