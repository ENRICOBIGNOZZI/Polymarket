"""Exact N-leg transport/hold/response scenarios, never venue attestations.

Market-specific mandatory holds are separate from transport. A supplied mapping
must cover EVERY basket token; omission is unknown, not zero. The legacy absent
mapping is retained only as an explicitly unverified zero-hold upper bound.
Public metadata acquisition/validity is a separate control-plane concern.
"""
from fractions import Fraction

from v7_unified_exact_arb_graph import GraphError, frac, fstr


def duration(value):
    result = frac(value)
    if not 0 <= result <= 60000 or (result * 1000000).denominator != 1:
        raise GraphError("execution_timing_duration")
    return result


def normalize_delay_profile(profile):
    if profile is None:
        return None
    if not isinstance(profile, dict) or not 1 <= len(profile) <= 128:
        raise GraphError("execution_delay_profile_capacity")
    if any(not isinstance(token, str) or not token or len(token) > 128 for token in profile):
        raise GraphError("execution_delay_profile_token")
    return {token: fstr(duration(value)) for token, value in sorted(profile.items())}


def candidate_delay_profile(candidate, override=None):
    supplied=normalize_delay_profile(override)
    if candidate.get("require_venue_terms") is not True:
        return supplied
    terms=candidate.get("venue_terms")
    if not isinstance(terms,dict) or terms.get("state")!="RECORDED_PUBLIC_TERMS":
        reason=terms.get("reason") if isinstance(terms,dict) else "missing"
        raise GraphError("venue_candidate_terms_unavailable:"+str(reason))
    recorded=normalize_delay_profile(terms.get("venue_delay_ms_by_token"))
    tokens={leg["token_id"] for leg in candidate["relation"]["legs"]}
    if recorded is None or set(recorded)!=tokens:
        raise GraphError("venue_candidate_delay_membership")
    if supplied is not None and any(supplied.get(token)!=recorded[token] for token in tokens):
        raise GraphError("venue_candidate_delay_override_conflict")
    return recorded


def timing_plan(tokens, detected, mode, delay_ms, skew_ms, unwind_delay_ms,
                venue_delay_ms_by_token=None, ack_delay_ms=0):
    """Known-at-decision schedule; ACK duration remains a scenario assumption.

    Sequential submission waits for the previous modeled matching result plus
    response travel, then skew. Parallel/batch legs can match out of index order.
    Unwind limits are all frozen when the last entry result is known. Unwind
    orders incur their OWN transport and same token-specific hold again.
    """
    if mode not in {"SEQUENTIAL", "PARALLEL", "BATCH"}:
        raise GraphError("execution_timing_mode")
    if not 1 <= len(tokens) <= 16 or len(set(tokens)) != len(tokens):
        raise GraphError("execution_timing_tokens")
    detected = frac(detected)
    delay, skew, unwind, ack = map(duration, (delay_ms, skew_ms, unwind_delay_ms, ack_delay_ms))
    profile = normalize_delay_profile(venue_delay_ms_by_token)
    if profile is not None and any(token not in profile for token in tokens):
        raise GraphError("execution_mandatory_delay_unknown")
    rows = []
    next_submit = detected
    for index, token in enumerate(tokens):
        hold = Fraction(0) if profile is None else frac(profile[token])
        submit = (next_submit if mode == "SEQUENTIAL" else
                  detected + index * skew if mode == "PARALLEL" else detected)
        wire = submit + delay
        match = wire + hold
        result = match + ack
        rows.append({"leg_index": index, "token_id": token, "submission_ms": submit,
                     "wire_arrival_ms": wire, "match_ms": match, "result_ms": result,
                     "venue_delay_ms": hold})
        next_submit = result + skew
    unwind_submit = max(row["result_ms"] for row in rows)
    for row in rows:
        row["unwind_submission_ms"] = unwind_submit
        row["unwind_wire_arrival_ms"] = unwind_submit + unwind
        row["unwind_match_ms"] = unwind_submit + unwind + row["venue_delay_ms"]
        row["unwind_result_ms"] = row["unwind_match_ms"] + ack
    return {"legs": rows, "entry_result_ms": unwind_submit,
            "finish_ms": max(row["unwind_result_ms"] for row in rows),
            "venue_delay_ms_by_token": profile, "ack_delay_ms": fstr(ack),
            "venue_delay_policy": "EXPLICIT_PER_TOKEN_SCENARIO" if profile is not None else
                                  "ZERO_HOLD_UPPER_BOUND_UNVERIFIED"}
