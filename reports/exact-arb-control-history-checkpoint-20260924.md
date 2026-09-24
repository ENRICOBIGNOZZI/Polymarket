# Native control-plane continuity checkpoint

Branch: `research/unified-exact-arb-graph`.
Base SHA: `f50147e18e02ebed76cabe900b708e4d9479e603` plus uncommitted changes.
PAPER / shadow only. No orders, deployment, promotion or champion economics changes.

## Correctness gap

The prior native arrival view reconstructed causal public books but copied fee
and relation terms from the candidate for the rest of its source lease. A later
local source failure, changed metadata or graph replacement was not present in
that book tape. Fresh books could therefore produce conditional fills using
already-invalidated local operands. That is not evidence of executable economics.

## Native producer

The dedicated graph observer now writes `native_exact_arb_control.jsonl` from
its single CONTROL thread. Records are model/session/manifest-bound, sequenced,
monotonic and chained by the SHA256 of the exact previous serialized record:

- `INVALIDATE`: initial unavailable state, source revalidation/failure, or stop.
- `ADMIT`: verified local bundle identity and its bounded monotonic source lease.
- `CHECKPOINT`: the control writer has recorded its prefix through this instant.

Invalidation cancels the runtime's old and pending generation. Changed source
bytes invalidate before validation; the former success cannot bridge validation
failure. Admission is flushed before publishing its owned native generation.
A compact admission-sequence handle is copied into each native observation and
full-evidence record. The feed performs no new serialization, hashing or file I/O.
The journal rotates at 64 MiB like the other native tapes. Journal failure is
sticky until observer restart, preventing a later heartbeat from certifying an
unrecorded transition. Native status reports journal failure/count/hash/watermark;
canonical graph health fails closed on missing or failed journal evidence.

The frame evaluator checks revocation between affected relations and again
before emitting. A frame timestamp predating the admitted generation cannot use
that generation. The existing owned-generation handoff and control-side storage
reclamation remain intact.

## Runner / replay admission

`v7_exact_arb_control_history.py` validates a complete supplied control prefix,
including its first unavailable record and final checkpoint. The native study
runner accepts chronological sealed segments via `--control-events`, archives
their exact records and binds their chain/receipt into the study identity.
There is no reconstruction of missing past control events from a later status.

Each execution view pins the candidate's exact admission sequence, bundle and
source deadline. Both decision start and compute end must lie inside that
admission interval. Every entry-arrival, unwind-submission and unwind-arrival
lookup is independently checked against the earliest invalidation, replacement,
lease expiry or control-prefix watermark. Intervals are half-open; an equal-time
or unobserved boundary cannot grant a fill.

A later admission cannot revive an old candidate, even if its bundle is byte-for-
byte identical. A new candidate carrying the new admission can be evaluated.
Missing control evidence or legacy observations without an admission reference
are explicitly censored, not granted a fallback to static candidate fees.

The same guard wraps independent and shared event-ordered execution. An observed
first-leg fill survives in the evidence when metadata becomes unavailable before
the second leg. Remaining fills, exposure and PnL are unknown; no partial fill is
retroactively erased and no unobserved order is assumed unfilled.

## Evidence and limitations

Adversarial tests cover absent control evidence, source timeout between WS frames,
invalidation between legs, later readmission, new-candidate recovery, lease or
identity mismatch, missing/reordered/duplicated records, altered hashes, backward
clocks, malformed authority, incomplete prefixes and immutable-artifact corruption.
The actual C++ producer's control wires are also consumed by the Python validator;
native tests cover mid-frame revocation, admission-time causality and concurrent
generation/admission-handle ownership. Execution identities also bind the exact
candidate admission interval and original full-evidence digest; differently
admitted candidates cannot silently share a cycle identity.

Final verification:

- **2,552 Python tests passed**, 1 existing skip, 384 existing numerical warnings.
- **410/410 configured Release CTest tests passed**.
- Changed native runtime/control targets and observer rebuilt in Release, Debug,
  combined ASan/UBSan and TSan. The five scoped native/roundtrip tests passed in
  all variants; both cross-language integrations passed again after the final
  Python provenance change.
- `git diff --check` passes; frozen champion lane, economics and multi-engine
  files have no diff. Existing observer compiler warnings remain unchanged.

Unrelated native targets were cached. These local results are not a clean Linux
release/security gate, a London latency benchmark or economic evidence.

This establishes **local recorded control-plane eligibility**, not independently
verified current venue fee semantics, ACK/cancel behavior, fill fragmentation,
NegRisk completeness, capital release or profitability. Source polling and source
leases still bound what is knowable. Revalidation blackouts and censored intervals
must be attributed to implementation/data coverage, not counted as negative
economic opportunities. The producer's unobserved tail remains unknown.

Hourly multi-session orchestration, independent execution/semantic terms, global
sizing/champion parity, capital lifecycle, maker calibration, true champion load
benchmarks, clean official release gates and the bounded London economic study
remain open. No fresh London receipt was obtained in this increment; the previous
read-only SSH attempt failed authentication. This is not evidence that London is down.

`research_decision = null`; the complete objective remains active and unfinished.
