"""Off-path native diagnostic episode -> pinned generic N-leg scenario input.

This does NOT fabricate an arrival history from positive-only full evidence.
Continuous native book replay and resource/venue admission remain separate gates.
"""
from fractions import Fraction
import hashlib

from v7_exact_arb_native_evidence import boundary, canonical, integer
from v7_unified_exact_arb_graph import SAFETY, GraphError, fstr, prove, sha


def rational(pair):
    if not isinstance(pair,list) or len(pair) != 2:
        raise GraphError("native_rational")
    if any(not isinstance(x,str) or len(x)>65 or str(int(x))!=x for x in pair):
        raise GraphError("native_rational")
    if int(pair[1]) <= 0: raise GraphError("native_rational")
    return Fraction(int(pair[0]),int(pair[1]))


def depth(raw, tick, descending):
    if not isinstance(raw,list) or len(raw)>1024: raise GraphError("native_depth")
    result=[]; previous=None
    for level in raw:
        if not isinstance(level,list) or len(level)!=2: raise GraphError("native_depth")
        p,q=level
        if type(p) is not int or type(q) is not int or not 0<p<10000 or not 0<q<2**63 or p%tick:
            raise GraphError("native_depth_level")
        if previous is not None and (p>=previous if descending else p<=previous):
            raise GraphError("native_depth_order")
        result.append([fstr(Fraction(p,10000)),fstr(Fraction(q,1000000))]);previous=p
    return result


def native_candidate(full, episode, bundles, model_sha, require_venue_terms=False):
    """Use ONLY episode start facts, never future lifetime/closure to select it."""
    boundary(full); boundary(episode)
    if type(require_venue_terms) is not bool: raise GraphError("venue_terms_mode")
    if (full.get("schema")!="polymarket_v7_native_exact_arb_full_evidence_v2"
        or full.get("model_sha")!=model_sha or full.get("evaluation_accepted") is not True
        or full.get("order_quantity_precision_ready") is not True
        or full.get("evidence_scope")!="CAUSAL_FRAME_END_PRE_ALLOCATION_EVALUATION"
        or full.get("actionable") is not False or full.get("economic_execution_verified") is not False):
        raise GraphError("native_candidate_identity")
    if (episode.get("schema")!="polymarket_v7_native_exact_arb_diagnostic_episode_v1"
        or episode.get("model_sha")!=model_sha or episode.get("stage")!="pre_allocation"
        or episode.get("left_censored") is not False or episode.get("economic_execution_verified") is not False):
        raise GraphError("native_episode_not_observed_start")
    for left,right in (("observer_session_id","observer_session_id"),
                       ("native_bundle_sha256","native_bundle_sha256"),
                       ("first_observation_sequence","observation_sequence"),
                       ("first_positive_ns","decision_start_ns")):
        if episode.get(left)!=full.get(right): raise GraphError("native_episode_join")
    session=full.get("observer_session_id")
    if not isinstance(session,str) or not session: raise GraphError("native_session")
    sequence=integer(full,"observation_sequence",1)
    feed_sequence=integer(full,"feed_frame_sequence",1)
    manifest=full.get("session_manifest_sha256")
    if not isinstance(manifest,str) or len(manifest)!=64 or any(c not in "0123456789abcdef" for c in manifest):
        raise GraphError("native_session_manifest")
    now=integer(full,"decision_start_ns",1)
    end=integer(full,"decision_end_ns",1)
    receive=integer(full,"receive_monotonic_ns",1)
    if not receive<=now<=end: raise GraphError("native_decision_clock")
    source_deadline=integer(full,"source_valid_until_monotonic_ns",1)
    relation_deadline=integer(full,"relation_valid_until_monotonic_ns",1)
    evaluation_deadline=integer(full,"evaluation_valid_until_monotonic_ns",1)
    if not now<=evaluation_deadline<=min(source_deadline,relation_deadline):
        raise GraphError("native_evaluation_deadline")
    body=bundles.document(full)
    native=body["relations"][full["relation_handle"]]
    family=native["relation_family"]+(":SELL" if native["sell_inventory"] else ":BUY")
    identity=[model_sha,session,sequence,(native["economic_identity"],family),"pre_allocation"]
    if (episode.get("economic_identity")!=native["economic_identity"] or episode.get("family")!=family
        or episode.get("episode_id")!=hashlib.sha256(canonical(identity).encode()).hexdigest()):
        raise GraphError("native_episode_identity")
    nodes={n["node_handle"]:n for n in body["nodes"]}
    if len(nodes)!=len(body["nodes"]): raise GraphError("native_node_collision")
    raw_books=full.get("leg_books")
    if not isinstance(raw_books,list) or len(raw_books)!=len(native["legs"]): raise GraphError("native_missing_full_evidence")
    books={b["token_id"]:b for b in raw_books}
    if any(not 1<=integer(b,"book_handle",1)<=128 for b in raw_books): raise GraphError("native_book_handle")
    if len(books)!=len(raw_books) or len({b["book_handle"] for b in raw_books})!=len(raw_books):
        raise GraphError("native_book_collision")
    q=integer(full,"quantity_microunits",1)
    quantum=integer(body,"order_share_quantum_microunits",1)
    epoch=integer(full,"connection_epoch",1)
    continuity=integer(full,"continuity_serial",1)
    lineage=canonical([session,epoch,continuity,full["native_bundle_sha256"]])
    versions=full.get("leg_versions")
    if not isinstance(versions,list) or len(versions)<len(native["legs"]): raise GraphError("native_leg_versions")
    anchors={};legs=[]
    for i,leg in enumerate(native["legs"]):
        token=nodes[leg["node_handle"]]["token_id"]
        if token not in books: raise GraphError("native_token_binding")
        book=books[token]; coefficient=rational(leg["coefficient"])
        shares=q*coefficient
        if coefficient<=0 or shares.denominator!=1 or shares.numerator%quantum or shares>=2**63:
            raise GraphError("native_order_precision")
        tick=integer(leg,"tick_size_e4",1)
        stamp=integer(book,"receive_monotonic_ns",1)
        if stamp>receive or book.get("valid") is not True or book.get("lineage_continuous") is not True:
            raise GraphError("native_book_causality")
        if book.get("tick_size_e4")!=tick or book.get("state_version")!=versions[i] or type(versions[i]) is not int or versions[i]<=0:
            raise GraphError("native_book_terms_or_version")
        # An unavailable opposite side is kept as truncated, not fabricated as
        # unwindable. The generic simulator conservatively censors this scenario.
        for key in ("ask_truncated","bid_truncated"):
            if type(book.get(key)) is not bool: raise GraphError("native_depth_completeness")
        rate=integer(leg,"fee_rate_nanos")
        exponent=integer(leg,"fee_exponent")
        if rate>1000000000 or exponent>2: raise GraphError("native_fee")
        minimum=integer(leg,"minimum_order_microunits")
        if shares<minimum: raise GraphError("native_minimum_order")
        anchors[token]={"timestamp_ms":fstr(Fraction(stamp,1000000)),
            "observation_ms":fstr(Fraction(now,1000000)),"lineage_id":lineage,"lineage_continuous":True,
            "state_version":book["state_version"],"depth_truncated":book["ask_truncated"] or book["bid_truncated"],
            "tick_size":fstr(Fraction(tick,10000)),"fee_rate":fstr(Fraction(rate,1000000000)),
            "fee_exponent":str(exponent),"asks":depth(book["asks_e4_microshares"],tick,False),
            "bids":depth(book["bids_e4_microshares"],tick,True)}
        if anchors[token]["asks"] and anchors[token]["bids"] and (
            book["bids_e4_microshares"][0][0]>=book["asks_e4_microshares"][0][0]):
            raise GraphError("native_crossed_book")
        legs.append({"token_id":token,"coefficient":fstr(coefficient),"minimum_order":fstr(Fraction(minimum,1000000)),
            "tick_size":fstr(Fraction(tick,10000)),"fee_rate":fstr(Fraction(rate,1000000000)),"fee_exponent":str(exponent),
            "fee_rounding_increment":"1/100000","fee_rounding_mode":"VENUE_5DP",
            "payout_vector":[fstr(rational(p)) for p in leg["payout_vector"]]})
    relation={"relation_id":native["relation_id"],"relation_family":native["relation_family"],
        "enabled":True,"relation_type":"CONSTANT_PAYOUT_EQUALITY","states":native["states"],"legs":legs,
        "guaranteed_payout":fstr(Fraction(integer(native,"guaranteed_payout_microunits",1),1000000)),
        "reserve_per_unit":fstr(Fraction(integer(native,"reserve_per_unit_microunits"),1000000)),
        "settlement_close_ms":fstr(Fraction(min(source_deadline,relation_deadline),1000000))}
    prove(relation)
    venue_terms=None
    if require_venue_terms:
        from v7_exact_arb_venue_selection import admitted_terms
        try: venue_terms=admitted_terms(full,body,bundles.directory)
        except (OSError,ValueError,TypeError,KeyError) as error:
            venue_terms={"state":"UNAVAILABLE","reason":str(error),"venue_execution_verified":False}
    candidate={"schema":"polymarket_v7_native_exact_arb_execution_candidate_v1",**SAFETY,
        "execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY","model_sha":model_sha,
        "graph_generation":full["graph_generation"],"native_bundle_sha256":full["native_bundle_sha256"],
        "observer_session_id":session,"connection_epoch":epoch,"continuity_serial":continuity,
        "feed_frame_sequence":feed_sequence,"session_manifest_sha256":manifest,
        "control_admission_sequence":integer(full,"control_admission_sequence",1) if "control_admission_sequence" in full else None,
        "source_valid_until_monotonic_ns":source_deadline,
        "selection_receipt_sha256":full.get("selection_receipt_sha256"),
        "require_venue_terms":require_venue_terms,"venue_terms":venue_terms,
        "observation_sequence":sequence,"opportunity_id":episode["episode_id"],
        "decision_timestamp_ms":fstr(Fraction(now,1000000)),
        "timestamp_ms":fstr(Fraction(end,1000000)),"time_basis":"SESSION_MONOTONIC_NS_AS_EXACT_RATIONAL_MS",
        "decision_books":anchors,"decision_books_sha256":sha(anchors),"relation":relation,
        "source_full_evidence_sha256":hashlib.sha256(canonical(full).encode()).hexdigest(),
        "inventory_reserved":False,"resource_admission_verified":False,"venue_execution_verified":False,
        "global_size_optimum_proven":full.get("global_size_optimum_proven") is True,
        "sizing_model":full.get("sizing_model"),
        "sizing_proof_scope":full.get("sizing_proof_scope"),
        "sizing_search_exhausted":full.get("sizing_search_exhausted"),
        "sizing_net_upper_bound_microunits":full.get("sizing_net_upper_bound_microunits"),
        "result":{"quantity":fstr(Fraction(q,1000000)),"direction":"SELL" if native["sell_inventory"] else "BUY",
                  "net_locked_pnl":fstr(Fraction(integer(full,"after_reserve_pnl_microunits",1),1000000))}}
    # Slow computation is a measured non-executable result, not malformed
    # evidence. Check it only AFTER validating all candidate inputs so a caller
    # can censor expiration without concealing corrupted full evidence.
    if end>=evaluation_deadline: raise GraphError("native_decision_expired")
    return candidate
