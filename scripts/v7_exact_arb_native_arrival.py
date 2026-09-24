"""Bounded OFFLINE history of whole-frame native WS replay output.

This is not a second WS decoder. Only the native replay's final frame snapshots
enter the history. Candidate views join the exact producer frame and book state
before translating replay continuity into the candidate's lineage namespace.
Rows remain provisional until the native output-chain receipt is verified.
No claim of venue fillability, complete producer tail, or resource admission.
"""
from copy import deepcopy
from fractions import Fraction
import hashlib
import json
import sqlite3

from v7_exact_arb_native_evidence import SAFETY, boundary, integer
from v7_exact_arb_native_execution_bridge import depth
from v7_unified_exact_arb_graph import GraphError, frac, fstr, sha


def require(ok, reason):
    if not ok:
        raise GraphError("native_arrival_"+reason)


class NativeArrivalHistory:
    def __init__(self, manifest_bytes, model, database=":memory:", max_frames=4096,
                 max_bytes=64*1024*1024):
        require(type(max_frames) is int and max_frames>0 and type(max_bytes) is int and max_bytes>0, "budget")
        require(len(manifest_bytes)<=1024*1024, "manifest_size")
        manifest=json.loads(manifest_bytes)
        boundary(manifest)
        require(manifest.get("schema")=="polymarket_v7_native_exact_arb_ws_session_v1"
                and manifest.get("model_sha")==model and len(model)==40
                and manifest.get("capture_scope")=="PUBLIC_WS_FRAMES_NOT_REST"
                and manifest.get("decoder_output_capacity")==512
                and manifest.get("maximum_frame_bytes")==1024*1024, "manifest")
        self.model=model
        self.session=manifest.get("observer_session_id")
        require(isinstance(self.session,str) and bool(self.session), "session")
        self.manifest_hash=hashlib.sha256(manifest_bytes).hexdigest()
        tokens=manifest.get("bindings")
        require(isinstance(tokens,list) and 0<len(tokens)<=128, "bindings")
        self.bindings={}
        handles=set()
        for token in tokens:
            tid=token.get("token_id")
            handle=integer(token,"book_handle",1);tick=integer(token,"tick_size_e4",1)
            require(isinstance(tid,str) and 0<len(tid)<=256 and tid not in self.bindings
                    and handle<=128 and handle not in handles and tick<10000, "binding")
            self.bindings[tid]=(handle,tick);handles.add(handle)
        self.max_frames,self.max_bytes=max_frames,max_bytes
        self.db=sqlite3.connect(database)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE frames(seq INTEGER PRIMARY KEY, available INTEGER, epoch INTEGER,
                continuity INTEGER, valid INTEGER, comparable INTEGER, decision INTEGER, bytes INTEGER, gap INTEGER);
            CREATE INDEX frame_time ON frames(available,seq);
            CREATE TABLE books(token TEXT, seq INTEGER REFERENCES frames(seq) ON DELETE CASCADE,
                body TEXT, PRIMARY KEY(token,seq));
        """)
        self.sequence=self.available=self.receive=self.epoch=self.continuity=0
        self.frames=self.missing=self.invalid=self.retained_bytes=self.retained_frames=0
        self.comparable=True
        self.chain=""
        self.failed=self.sealed=False

    def close(self):
        self.db.close()

    @property
    def watermark(self):
        require(not self.failed, "failed_history")
        return Fraction(self.available,1000000)

    def receipt_verified(self):
        return self.sealed and not self.failed

    def evidence_identity(self):
        return {"model_sha":self.model,"observer_session_id":self.session,
            "session_manifest_sha256":self.manifest_hash,"output_chain_sha256":self.chain,
            "frames_replayed":self.frames,"availability_monotonic_ns":self.available,
            "replay_receipt_verified":self.receipt_verified(),
            "producer_tail_completeness_verified":False,"venue_execution_verified":False}

    def _identity(self,row,schema):
        boundary(row)
        require(row.get("schema")==schema and row.get("model_sha")==self.model
                and row.get("observer_session_id")==self.session
                and row.get("session_manifest_sha256")==self.manifest_hash, "identity")

    def ingest(self, serialized):
        require(not self.failed and not self.sealed, "closed_history")
        try:
            require(isinstance(serialized,str) and len(serialized.encode())<=8*1024*1024, "row_size")
            row=json.loads(serialized)
            self._identity(row,"polymarket_v7_native_exact_arb_replayed_frame_v1")
            seq=integer(row,"feed_frame_sequence",1)
            available=integer(row,"availability_monotonic_ns",1)
            receive=integer(row,"receive_monotonic_ns",1)
            # Older replay artifacts lack this field; never reconstruct UTC
            # from a session name, venue timestamp or analysis-machine clock.
            if "receive_wall_ms" in row: integer(row,"receive_wall_ms",1)
            decision=integer(row,"graph_decision_start_ns",1)
            epoch=integer(row,"connection_epoch",1)
            continuity=integer(row,"replay_continuity_serial",1)
            gap=integer(row,"missing_frames_before")
            valid=row.get("source_frame_valid");comparable=row.get("source_state_versions_comparable")
            require(type(valid) is bool and type(comparable) is bool, "booleans")
            require(seq>self.sequence and gap==seq-self.sequence-1 and epoch>=self.epoch
                    and receive>=self.receive and available>=self.available
                    and receive<=available<=decision, "sequence_or_clock")
            reset="INITIAL_SESSION" if not self.sequence else ""
            if gap: reset="FEED_SEQUENCE_GAP"
            if self.epoch and epoch!=self.epoch: reset="CONNECTION_EPOCH_CHANGED"
            if not valid: reset="SOURCE_FRAME_INVALID"
            require(row.get("reset_reason")==reset and continuity==self.continuity+bool(reset), "continuity")
            require(not comparable or (self.comparable and not gap), "invented_versions")
            books=row.get("books")
            require(isinstance(books,list) and len(books)<=len(self.bindings) and (valid or not books), "books")
            seen=set(); stored=[]
            for book in books:
                token=book.get("token_id")
                require(token in self.bindings and token not in seen, "book_binding")
                seen.add(token)
                handle,tick=self.bindings[token]
                require(book.get("book_handle")==handle and book.get("tick_size_e4")==tick, "book_terms")
                require(integer(book,"receive_monotonic_ns")<=receive, "future_book")
                integer(book,"state_version")
                for field in ("valid","lineage_continuous","ask_truncated","bid_truncated"):
                    require(type(book.get(field)) is bool, "book_boolean")
                # Validate depths even on unusable books; never sort/repair them.
                depth(book.get("asks_e4_microshares"),tick,False)
                depth(book.get("bids_e4_microshares"),tick,True)
                stored.append((token,seq,json.dumps(book,separators=(",",":"))))
            size=len(serialized.encode())
            require(size<=self.max_bytes, "frame_exceeds_budget")
            with self.db:
                self.db.execute("INSERT INTO frames VALUES(?,?,?,?,?,?,?,?,?)",
                    (seq,available,epoch,continuity,valid,comparable,decision,size,gap))
                self.db.executemany("INSERT INTO books VALUES(?,?,?)",stored)
                self.retained_bytes+=size;self.retained_frames+=1
                while self.retained_frames>self.max_frames or self.retained_bytes>self.max_bytes:
                    old,amount=self.db.execute("SELECT seq,bytes FROM frames ORDER BY seq LIMIT 1").fetchone()
                    self.db.execute("DELETE FROM frames WHERE seq=?",(old,))
                    self.retained_frames-=1;self.retained_bytes-=amount
            self.sequence,self.available,self.receive=seq,available,receive
            self.epoch,self.continuity,self.comparable=epoch,continuity,comparable
            self.frames+=1;self.missing+=gap;self.invalid+=not valid
            self.chain=hashlib.sha256((self.chain+serialized).encode()).hexdigest()
        except Exception:
            self.failed=True
            raise

    def seal(self, serialized_receipt):
        require(not self.failed and not self.sealed, "closed_history")
        try:
            require(len(serialized_receipt)<=16384, "receipt_size")
            row=json.loads(serialized_receipt)
            self._identity(row,"polymarket_v7_native_exact_arb_ws_replay_receipt_v1")
            require(row.get("state")=="INPUT_REPLAYED" and row.get("output_chain_sha256")==self.chain
                    and row.get("producer_tail_completeness_verified") is False
                    and row.get("venue_execution_verified") is False, "receipt")
            for name,value in (("frames_replayed",self.frames),("missing_frames",self.missing),
                               ("invalid_frames",self.invalid),("last_feed_frame_sequence",self.sequence),
                               ("availability_monotonic_ns",self.available)):
                require(integer(row,name)==value, "receipt_counts")
            self.sealed=True
        except Exception:
            self.failed=True
            raise

    def _book(self, token, seq, continuity):
        found=self.db.execute("""SELECT b.body,f.available,f.continuity FROM books b
            JOIN frames f ON f.seq=b.seq WHERE b.token=? AND b.seq<=? ORDER BY b.seq DESC LIMIT 1""",
            (token,seq)).fetchone()
        if found is None or found[2]!=continuity:
            return None
        book=json.loads(found[0])
        if not book["valid"] or not book["lineage_continuous"] or not book["receive_monotonic_ns"]:
            return None
        return book,found[1]

    def for_candidate(self, candidate):
        require(not self.failed, "failed_history")
        return CandidateArrivalView(self,candidate)


class CandidateArrivalView:
    def __init__(self, history, candidate):
        self.history=history;self.candidate=deepcopy(candidate)
        require(all(candidate.get(k) is v for k,v in SAFETY.items() if k!="execution_authority")
                and candidate.get("execution_authority")=="ZERO_AUTHORITY_RESEARCH_ONLY", "candidate_safety")
        require(candidate.get("schema")=="polymarket_v7_native_exact_arb_execution_candidate_v1"
                and candidate.get("model_sha")==history.model
                and candidate.get("observer_session_id")==history.session
                and candidate.get("session_manifest_sha256")==history.manifest_hash, "candidate_identity")
        self.sequence=integer(candidate,"feed_frame_sequence",1)
        row=history.db.execute("SELECT epoch,continuity,valid,comparable,decision,available FROM frames WHERE seq=?",
                               (self.sequence,)).fetchone()
        require(row is not None, "candidate_frame_evicted_or_missing")
        require(row[0]==candidate.get("connection_epoch") and row[2] and row[3]
                and Fraction(row[4],1000000)==frac(candidate["decision_timestamp_ms"])
                and row[5]<=row[4], "candidate_frame_join")
        self.continuity=row[1];self.epoch=row[0]
        anchors=candidate.get("decision_books")
        require(isinstance(anchors,dict) and candidate.get("decision_books_sha256")==sha(anchors), "candidate_books")
        for token,anchor in anchors.items():
            found=history._book(token,self.sequence,self.continuity)
            require(found is not None, "candidate_book_missing")
            converted=self._convert(token,*found)
            converted["observation_ms"]=candidate["decision_timestamp_ms"]
            require(converted==anchor, "candidate_book_mismatch")

    @property
    def watermark(self):
        return self.history.watermark if self.replay_receipt_verified else Fraction(-1)

    @property
    def replay_receipt_verified(self):
        return self.history.receipt_verified()

    @property
    def evidence_identity(self):
        return self.history.evidence_identity()

    def _convert(self,token,book,available):
        anchor=self.candidate["decision_books"][token]
        tick=self.history.bindings[token][1]
        return {"timestamp_ms":fstr(Fraction(book["receive_monotonic_ns"],1000000)),
            "observation_ms":fstr(Fraction(available,1000000)),"lineage_id":anchor["lineage_id"],
            "lineage_continuous":True,"state_version":book["state_version"],
            "depth_truncated":book["ask_truncated"] or book["bid_truncated"],
            "tick_size":fstr(Fraction(tick,10000)),"fee_rate":anchor["fee_rate"],
            "fee_exponent":anchor["fee_exponent"],"asks":depth(book["asks_e4_microshares"],tick,False),
            "bids":depth(book["bids_e4_microshares"],tick,True)}

    def at(self,token,timestamp):
        h=self.history
        require(not h.failed, "failed_history")
        if not self.replay_receipt_verified:
            return None
        ns=frac(timestamp)*1000000
        if token not in self.candidate["decision_books"] or ns>h.available or ns<0:
            return None
        # Floor only the SQL bound, not the requested instant. Native times are
        # integer ns; no row after the exact rational query can become visible.
        row=h.db.execute("SELECT seq,epoch,continuity,valid,comparable FROM frames WHERE available<=? "
                         "ORDER BY available DESC,seq DESC LIMIT 1",(ns.numerator//ns.denominator,)).fetchone()
        if row is None or row[0]<self.sequence or row[1]!=self.epoch or row[2]!=self.continuity or not all(row[3:]):
            return None
        # A later recorded frame can reveal producer loss in the interval being
        # queried. Missing frames have UNKNOWN availability times; censor the
        # entire preceding interval, not only times after the gap is discovered.
        following=h.db.execute("SELECT gap FROM frames WHERE seq>? ORDER BY seq LIMIT 1",(row[0],)).fetchone()
        if following is not None and following[0]:
            return None
        found=h._book(token,row[0],self.continuity)
        return self._convert(token,*found) if found else None
