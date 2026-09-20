"""Action evidence from immutable post-decision native capture windows."""
from collections import defaultdict

from .catalog import NATIVE, CLOSED
from .common import canonical, digest, source_rows
from .execution import execution_label
from v7_evidence_store import EvidenceStore
from research.economic.causal_replay import Book, BookTape, CausalReplay, Order, EvidenceError


def replay_actions(store_root, revisions, decisions, *, transport_delay_ns, quantity_shares,
                   execution_cost_per_share):
    """Explicit assumed transport/cost scenario. Returns labels, never ledger writes.

    Decision-window closure/sequence/epoch checks are mandatory. Each signal is
    a mutually exclusive counterfactual; policy evaluation must pick at most one
    action per market before totaling PnL. Cost assumptions remain in receipts.
    """
    if transport_delay_ns <= 0 or quantity_shares <= 0 or execution_cost_per_share is None or execution_cost_per_share < 0:
        raise ValueError("EXPLICIT_EXECUTION_SCENARIO_REQUIRED")
    captures = defaultdict(dict); closures = {}; origins = {}; source_refs = defaultdict(set)
    wanted = {r["source_record_sha256"]: r for r in decisions if r["stratum"] == "native_causal_features_v1"}
    with EvidenceStore(store_root) as store:
        for revision in sorted(set(revisions)):
            rev = store.revision(revision)
            if not ({NATIVE, CLOSED} & set(rev.get("contract", {}).get("schemas", {}))):
                continue
            # Closed receipts are single JSON objects; capture sources are JSONL.
            values = source_rows(store, revision)
            for row in values:
                if row.get("schema") not in {NATIVE, CLOSED}:
                    continue
                key = (row.get("server_id"), row.get("run_id"), row.get("capture_id"))
                if not all(key):
                    continue
                source_refs[key].add(revision)
                if row["schema"] == CLOSED:
                    if key in closures and closures[key] != row:
                        raise ValueError("CONFLICTING_CAPTURE_CLOSURE")
                    closures[key] = row; continue
                seq = row["sequence"]
                if seq in captures[key] and captures[key][seq] != row:
                    raise ValueError("CONFLICTING_CAPTURE_SEQUENCE")
                captures[key][seq] = row
                sha = digest(canonical(row))
                if sha in wanted:
                    origins[wanted[sha]["decision_id"]] = (key, row)
    tapes = {}
    for key, events in captures.items():
        closed = closures.get(key)
        ordered = [events[k] for k in sorted(events)]; sequences = sorted(events)
        if (not closed or closed.get("closed") is not True or closed.get("healthy") is not True
                or not sequences or sequences != list(range(1, sequences[-1]+1))
                or closed.get("last_sequence") != sequences[-1]):
            continue
        capture = "|".join(key); books = []; gaps = []; last = None
        for r in ordered:
            t = r["observed_monotonic_ns"]
            if last and (t < last["observed_monotonic_ns"] or r.get("connection_epoch") != last.get("connection_epoch")):
                gaps.append((last["observed_monotonic_ns"], t))
            if r["kind"] == 5:
                gaps.append((last["observed_monotonic_ns"] if last else t, t))
            if r["kind"] in (1, 3, 5) and r.get("token_id") and r.get("tick_e4", 0) > 0:
                bids = tuple(map(tuple, r.get("bids") or [])); asks = tuple(map(tuple, r.get("asks") or []))
                if not bids and r.get("bid_e4", 0) > 0 and r.get("bid_quantity") is not None:
                    bids = ((r["bid_e4"], r["bid_quantity"]),)
                if not asks and r.get("ask_e4", 0) > 0 and r.get("ask_quantity") is not None:
                    asks = ((r["ask_e4"], r["ask_quantity"]),)
                try:
                    books.append(Book(str(r["market_id"]), r["token_id"], capture, t, r["book_version"],
                                      bids, asks, r["tick_e4"], r.get("book_valid") is True, r["receive_monotonic_ns"]))
                except EvidenceError:
                    gaps.append((t, t))
            last = r
        tapes[key] = BookTape.from_books(books, {capture: closed["watermark_monotonic_ns"]}, {capture: gaps})
    labels = {}
    for row in decisions:
        labels[row["decision_id"]] = execution_label(row, None)
        if row["decision_id"] not in origins:
            continue
        key, raw = origins[row["decision_id"]]
        if key not in tapes:
            continue
        delay = raw.get("paper_venue_delay_ns"); minimum = raw.get("minimum_order_microunits")
        rate, exponent = raw.get("fee_rate"), raw.get("fee_exponent")
        if (type(delay) is not int or delay < 0 or not raw.get("paper_terms_sha256")
                or type(minimum) is not int or minimum <= 0 or rate is None or exponent is None):
            continue
        effective_delay = delay+transport_delay_ns
        # Older DECISIONS/WINDOWS captures do not prove continuous coverage
        # for every reject. Never infer a capture interval from a default TTL.
        if raw.get("capture_mode") != "FULL":
            continue
        replay = CausalReplay(tapes[key], rate=rate, exponent=exponent, max_order_cost=3.75)
        order = Order(row["decision_id"], row["market_id"], row["token_id"], "|".join(key),
                      raw["decision_monotonic_ns"], "BUY", raw["ask_e4"], int(quantity_shares*1e6),
                      minimum, effective_delay, False)
        result = replay.execute(order)
        if result.status not in {"FILLED", "PARTIAL", "PARTIAL_FILL", "NONFILL"}:
            continue
        q = result.filled_quantity/1e6
        # The counterfactual does not claim transaction price for an observed nonfill.
        arrival = {"decision_id": row["decision_id"], "coverage_verified": True,
                   "arrival_ns": row["decision_ns"]+effective_delay,
                   "information_ns": row["decision_ns"]+effective_delay,
                   "semantics": "ARRIVAL_TOP_FAK_ACTUAL_PRICE_PARTIAL_FILL_V2",
                   "source_revisions": sorted(source_refs[key]), "intended_quantity": quantity_shares,
                   "filled_quantity": q, "execution_price": result.average_price if q else 0.,
                   "fees": result.fee, "execution_cost": q*execution_cost_per_share,
                   "slippage": result.notional-q*raw["ask_e4"]/10000,
                   "action": f"FAK_{quantity_shares:g}_DELAY_{effective_delay}"}
        labels[row["decision_id"]] = {**execution_label(row, arrival),
            "cost_assumption": {"reserve_per_filled_share": execution_cost_per_share,
                                "measured": False}, "transport_delay_ns": transport_delay_ns}
    return labels
