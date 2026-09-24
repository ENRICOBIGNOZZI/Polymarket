"""Off-path public terms -> immutable native selection provenance.

Only a native ADMIT of the exact selection establishes session availability.
Wall timestamps bound a conservative local cache lease; they are not converted
into historical session timestamps and do not guarantee venue immutability.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil

from v7_exact_arb_venue_terms import collect, persist, validate_receipt, require, identity
from v7_unified_exact_arb_graph import SAFETY, GraphError

SCHEMA = "polymarket_v7_exact_arb_selection_venue_terms_v1"
MAX_AGE_MS = 30000
MAX_RECEIPT_BYTES = 8*1024*1024
MAX_ACTIVE_BYTES = 32*1024*1024


def selected_conditions(selection):
    result={};tokens=set()
    rows=selection.get("markets")
    require(isinstance(rows,list) and len(rows)<=64,"venue_selection_market_bounds")
    for row in rows:
        condition=row.get("condition_id");pair=[row.get("yes_token"),row.get("no_token")]
        identity(condition,pair)
        pair.sort()
        require(condition not in result and not tokens.intersection(pair),"venue_selection_identity_collision")
        result[condition]=pair;tokens.update(pair)
    return result


def terms_envelope(selection, receipts, now_ms):
    require(type(now_ms) is int and now_ms>0,"venue_selection_clock")
    rows=[];deadlines=[]
    for condition,tokens in sorted(selected_conditions(selection).items()):
        receipt=receipts.get(condition)
        row={"condition_id":condition,"token_ids":tokens,"snapshot_sha256":None,
             "state":"UNVERIFIED","reason":"venue_terms_not_collected","terms":None,
             "observed_start_wall_ns":None,"observed_end_wall_ns":None,"valid_until_wall_ms":None}
        if receipt is not None:
            row["snapshot_sha256"]=receipt["snapshot_sha256"]
            try:
                terms,start,end=validate_receipt(receipt)
                require(terms["condition_id"]==condition and terms["token_ids"]==tokens,"venue_selection_token_binding")
                until=start//1000000+MAX_AGE_MS
                require(end<=now_ms*1000000 and now_ms<until,"venue_selection_expired_or_future")
                row.update(state="OBSERVED_SUPPORTED_TERMS",reason=None,terms=terms,
                    observed_start_wall_ns=start,observed_end_wall_ns=end,valid_until_wall_ms=until)
                deadlines.append(until)
            except (GraphError,ValueError,TypeError,KeyError) as error:
                row["reason"]=str(error)
        rows.append(row)
    return {"schema":SCHEMA,**SAFETY,"execution_authority":False,"maximum_age_ms":MAX_AGE_MS,
            "scope":"PUBLIC_METADATA_KNOWN_AT_NATIVE_SELECTION_ADMISSION", "receipts":rows},deadlines


def attach_terms(selection, receipts, now_ms):
    result=deepcopy(selection)
    require(type(selection.get("timestamp_ms")) is int and selection["timestamp_ms"]<=now_ms,"venue_selection_clock")
    result["timestamp_ms"]=now_ms
    envelope,deadlines=terms_envelope(selection,receipts,now_ms)
    result["venue_terms"]=envelope
    if deadlines:
        result["valid_until_ms"]=min(result["valid_until_ms"],*deadlines)
    require(result["timestamp_ms"]<=now_ms<result["valid_until_ms"],"venue_selection_source_expired")
    return result


class VenueTermsCache:
    """Bounded public I/O pool; caller INVALIDATES publication before refresh.

    No restored success cache after restart, and no stale-on-error behavior.
    The caller republishes only after rechecking graph/universe source leases.
    """
    def __init__(self, directory, collector=collect, maximum_archive_bytes=8*1024**3):
        self.directory=Path(directory);self.collector=collector;self.receipts={};self.targets={};self.refresh_at=0
        self.maximum_archive_bytes=maximum_archive_bytes
        self.directory.mkdir(parents=True,exist_ok=True)
        self.archive_bytes=sum(p.stat().st_size for p in self.directory.iterdir() if p.is_file())

    def due(self, selection, now_ms):
        return selected_conditions(selection)!=self.targets or now_ms>=self.refresh_at

    def refresh(self, selection, now_ms):
        targets=selected_conditions(selection)
        # Forget successes BEFORE any requests; a raised error must not retain
        # old data while the publisher reports a failed refresh.
        self.receipts={};self.targets={};self.refresh_at=0
        active_bytes=0;found={}
        require(self.archive_bytes<self.maximum_archive_bytes,"venue_archive_budget")
        require(shutil.disk_usage(self.directory).free>=1024**3,"venue_archive_free_disk")
        with ThreadPoolExecutor(max_workers=4,thread_name_prefix="venue-public-metadata") as pool:
            jobs={condition:pool.submit(self.collector,condition,tokens) for condition,tokens in sorted(targets.items())}
            for condition,future in jobs.items():
                receipt=future.result()
                size=len(json.dumps(receipt,sort_keys=True,indent=2,allow_nan=False).encode())+1
                active_bytes+=size
                require(size<=MAX_RECEIPT_BYTES and active_bytes<=MAX_ACTIVE_BYTES,"venue_active_receipt_budget")
                target=self.directory/(receipt["snapshot_sha256"]+".json")
                added=0 if target.exists() else size
                require(self.archive_bytes+added<=self.maximum_archive_bytes,"venue_archive_budget")
                persist(self.directory,receipt);self.archive_bytes+=added
                found[condition]=receipt
        self.receipts,self.targets=found,targets
        self.refresh_at=now_ms+MAX_AGE_MS//2

    def attach(self, selection, now_ms):
        return attach_terms(selection,self.receipts,now_ms)


def read_receipt(directory, digest):
    require(isinstance(digest,str) and len(digest)==64 and all(c in "0123456789abcdef" for c in digest),"venue_receipt_digest_shape")
    path=Path(directory)/(digest+".json")
    require(path.is_file() and not path.is_symlink() and path.stat().st_size<=MAX_RECEIPT_BYTES,"venue_receipt_file")
    value=json.loads(path.read_text())
    require(value.get("snapshot_sha256")==digest,"venue_receipt_file_identity")
    return value


def admitted_terms(full, bundle, directory):
    """Revalidate archived receipt bytes and EXACT native selection digest.

    No fallbacks to the current publisher file, latest metadata or another
    selection using the same mathematical graph. Control journal binding is
    checked separately at each hypothetical match by ControlArrivalView.
    """
    digest=full.get("selection_receipt_sha256")
    require(isinstance(digest,str) and len(digest)==64 and all(c in "0123456789abcdef" for c in digest),"venue_selection_receipt_missing")
    path=Path(directory)/(digest+".selection.json")
    require(path.is_file() and not path.is_symlink() and path.stat().st_size<=8*1024*1024,"venue_selection_receipt_file")
    raw=path.read_bytes()
    require(hashlib.sha256(raw).hexdigest()==digest,"venue_selection_receipt_digest")
    selection=json.loads(raw)
    require(selection.get("schema")=="polymarket_v7_exact_arb_hotset_selection_v1" and
            selection.get("selection_only") is True and selection.get("source_actionable") is False and
            all(selection.get(k) is v for k,v in SAFETY.items()) and selection.get("execution_authority") is False and
            selection.get("source_valid") is True and selection.get("model_sha")==full["model_sha"] and
            selection.get("graph_generation")==full["graph_generation"] and
            selection.get("native_runtime_bundle",{}).get("sha256")==full["native_bundle_sha256"],"venue_selection_receipt_identity")
    envelope=selection.get("venue_terms")
    require(isinstance(envelope,dict) and envelope.get("schema")==SCHEMA and
            envelope.get("maximum_age_ms")==MAX_AGE_MS and all(envelope.get(k) is v for k,v in SAFETY.items()) and
            envelope.get("execution_authority") is False,"venue_selection_terms_missing")
    rows=envelope.get("receipts")
    require(isinstance(rows,list) and len(rows)<=64,"venue_selection_terms_bounds")
    by_condition={r["condition_id"]:r for r in rows}
    require(len(by_condition)==len(rows),"venue_selection_terms_collision")
    now=selection.get("timestamp_ms");expiry=selection.get("valid_until_ms")
    require(type(now) is int and type(expiry) is int and 0<now<expiry and
            expiry==full.get("source_valid_until_wall_ms"),"venue_selection_lease_binding")
    node_by_handle={n["node_handle"]:n for n in bundle["nodes"]}
    relation=bundle["relations"][full["relation_handle"]]
    conditions={node_by_handle[leg["node_handle"]].get("condition_id") for leg in relation["legs"]}
    require(None not in conditions,"venue_native_condition_missing")
    verified={};receipts={}
    for condition in conditions:
        row=by_condition.get(condition)
        require(isinstance(row,dict) and row.get("state")=="OBSERVED_SUPPORTED_TERMS","venue_condition_terms_unavailable")
        receipt=read_receipt(Path(directory).parent/"native_venue_terms",row.get("snapshot_sha256"))
        terms,start,end=validate_receipt(receipt)
        require(terms["condition_id"]==condition and terms==row.get("terms") and terms["token_ids"]==row.get("token_ids") and
                row.get("observed_start_wall_ns")==start and row.get("observed_end_wall_ns")==end and
                row.get("valid_until_wall_ms")==start//1000000+MAX_AGE_MS and
                end<=now*1000000<expiry*1000000<=row["valid_until_wall_ms"]*1000000,"venue_selection_terms_provenance")
        verified[condition]=terms;receipts[condition]=row["snapshot_sha256"]
    from v7_unified_exact_arb_graph import frac
    from fractions import Fraction
    profile={}
    for leg in relation["legs"]:
        node=node_by_handle[leg["node_handle"]];terms=verified[node["condition_id"]];token=node["token_id"]
        require(token in terms["token_ids"],"venue_native_token_binding")
        require(frac(terms["fee_rate"])==Fraction(leg["fee_rate_nanos"],1000000000) and
                frac(terms["fee_exponent"])==leg["fee_exponent"] and
                frac(terms["tick_size"])==Fraction(leg["tick_size_e4"],10000) and
                frac(terms["minimum_order_shares"])==Fraction(leg["minimum_order_microunits"],1000000),"venue_native_operand_mismatch")
        profile[token]=terms["mandatory_taker_delay_ms"]
    return {"state":"RECORDED_PUBLIC_TERMS","selection_receipt_sha256":digest,"receipt_sha256_by_condition":receipts,
            "venue_delay_ms_by_token":profile,"venue_execution_verified":False,
            "scope":"RECORDED_SELECTION_ADMISSION_NOT_GUARANTEED_MATCH_TIME_TERMS"}
