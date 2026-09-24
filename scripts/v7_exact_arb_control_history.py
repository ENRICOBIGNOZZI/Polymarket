"""Causal native metadata eligibility, separate from public book reconstruction.

The CONTROL-only producer journals invalidation before validation and persists
admission before publishing a generation. Its periodic checkpoint certifies only
the recorded prefix, never an unknown producer tail or actual venue semantics.
"""
import hashlib
import json

from v7_exact_arb_native_evidence import SAFETY, boundary, integer
from v7_unified_exact_arb_graph import GraphError, frac


class ControlHistory:
    def __init__(self, db, wires, model, session, manifest_hash):
        self.db=db; self.chain=""; self.sequence=self.watermark=0
        self.available=False
        db.execute("CREATE TABLE control_events(seq INTEGER PRIMARY KEY, stamp INTEGER, kind TEXT, bundle TEXT, deadline INTEGER, wire TEXT, selection TEXT)")
        db.execute("CREATE INDEX control_transition ON control_events(kind,seq)")
        previous_time=0; last_kind=None
        for wire in wires:
            if len(wire.encode())>16384: raise GraphError("control_record_size")
            row=json.loads(wire);boundary(row)
            if (row.get("schema")!="polymarket_v7_native_exact_arb_control_v1" or row.get("model_sha")!=model
                    or row.get("observer_session_id")!=session or row.get("session_manifest_sha256")!=manifest_hash):
                raise GraphError("control_record_identity")
            seq=integer(row,"sequence",1);stamp=integer(row,"timestamp_monotonic_ns",1)
            kind=row.get("kind");bundle=row.get("native_bundle_sha256");deadline=integer(row,"valid_until_monotonic_ns")
            selection=row.get("selection_receipt_sha256")
            if selection is not None:
                if (not isinstance(selection,str) or (kind=="ADMIT" and (len(selection)!=64 or
                        any(c not in "0123456789abcdef" for c in selection))) or (kind!="ADMIT" and selection!="")):
                    raise GraphError("control_selection_digest")
            if (seq!=self.sequence+1 or stamp<previous_time or row.get("previous_record_sha256")!=self.chain
                    or kind not in {"INVALIDATE","ADMIT","CHECKPOINT"} or (seq==1 and kind!="INVALIDATE")):
                raise GraphError("control_sequence_clock_or_hash")
            if kind=="ADMIT":
                if (not isinstance(bundle,str) or len(bundle)!=64 or any(c not in "0123456789abcdef" for c in bundle)
                        or not stamp<deadline<=stamp+120000000000 or last_kind=="ADMIT"):
                    raise GraphError("control_admission_terms")
            elif bundle!="" or deadline!=0: raise GraphError("control_nonadmission_terms")
            self.db.execute("INSERT INTO control_events VALUES(?,?,?,?,?,?,?)",(seq,stamp,kind,bundle,deadline,wire,selection))
            self.chain=hashlib.sha256(wire.encode()).hexdigest();self.sequence=seq;previous_time=stamp
            if kind!="CHECKPOINT": last_kind=kind
            self.available=kind=="CHECKPOINT"
            if self.available: self.watermark=stamp
        # Empty/missing input is explicitly unavailable, not an unlimited lease.
        if self.sequence and not self.available: raise GraphError("control_prefix_requires_checkpoint")

    def receipt(self):
        return {**SAFETY,"schema":"polymarket_v7_native_exact_arb_control_receipt_v1",
                "state":"CHECKPOINTED_PREFIX" if self.available else "UNAVAILABLE",
                "records":self.sequence,"last_record_sha256":self.chain or None,
                "watermark_monotonic_ns":self.watermark if self.available else None,
                "producer_tail_completeness_verified":False,"venue_semantics_verified":False}

    def view(self, books, candidate):
        return ControlArrivalView(books,self,candidate)


class ControlArrivalView:
    """Pin one admission interval; later recovery cannot revive old operands."""
    def __init__(self, books, control, candidate):
        self.books,self.control=books,control
        self.reason=None;self.end=None
        self.admitted_at=None;self.boundary_reasons=[]
        seq=candidate.get("control_admission_sequence")
        self.sequence=seq
        self.full_digest=candidate.get("source_full_evidence_sha256")
        if not control.available: self.reason="control_evidence_missing"
        elif type(seq) is not int or not 0<seq<2**63: self.reason="control_candidate_admission_missing"
        else:
            row=control.db.execute("SELECT stamp,kind,bundle,deadline,selection FROM control_events WHERE seq=?",(seq,)).fetchone()
            if row is None or row[1]!="ADMIT" or row[2]!=candidate["native_bundle_sha256"]:
                self.reason="control_candidate_admission_mismatch"
            else:
                self.admitted_at=row[0]
                next_change=control.db.execute("SELECT stamp,kind FROM control_events WHERE seq>? AND kind!='CHECKPOINT' ORDER BY seq LIMIT 1",(seq,)).fetchone()
                boundaries=[(row[3],"SOURCE_LEASE"),(control.watermark,"CONTROL_PREFIX_WATERMARK")]
                if next_change: boundaries.append((next_change[0],next_change[1]))
                self.end=min(stamp for stamp,_ in boundaries)
                self.boundary_reasons=[kind for stamp,kind in boundaries if stamp==self.end]
                start=frac(candidate["decision_timestamp_ms"])*1000000
                send=frac(candidate["timestamp_ms"])*1000000
                if (row[3]!=candidate.get("source_valid_until_monotonic_ns") or not row[0]<=start<=send<self.end):
                    self.reason="control_candidate_outside_admission"
                if candidate.get("require_venue_terms") is True and (
                        not row[4] or row[4]!=candidate.get("selection_receipt_sha256")):
                    self.reason="control_venue_selection_not_bound"
        self.start=frac(candidate["decision_timestamp_ms"])*1000000

    @property
    def watermark(self):
        return self.books.watermark

    @property
    def evidence_identity(self):
        return {**self.books.evidence_identity,"control_history":self.control.receipt(),
                "control_candidate_interval":{"admission_sequence":self.sequence,
                    "source_full_evidence_sha256":self.full_digest,
                    "eligible_from_monotonic_ns":self.admitted_at,"eligible_until_monotonic_ns":self.end,
                    "boundary_reasons":self.boundary_reasons,"admission_error":self.reason}}

    def at(self,token,timestamp):
        if self.reason: raise GraphError(self.reason)
        now=frac(timestamp)*1000000
        if now<self.start: raise GraphError("control_query_before_candidate")
        if now>=self.end: raise GraphError("control_admission_ended_or_unobserved")
        return self.books.at(token,timestamp)
