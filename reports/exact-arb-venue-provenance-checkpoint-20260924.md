# Native-admitted venue provenance checkpoint

Branch: `research/unified-exact-arb-graph`.
Base SHA: `f50147e18e02ebed76cabe900b708e4d9479e603` plus uncommitted changes.
PAPER only; zero execution authority. No deployment, merge or promotion.

## Closed local provenance gap

The previous venue-timing increment accepted explicit delay scenarios but had
no causal path from an observed public descriptor to a native candidate. This
increment connects the existing publisher, immutable selection archive, native
CONTROL admission and offline arrival guard; it does not map collector wall
timestamps retrospectively into the session clock.

The source bytes are collected and persisted before publication. The native
observer archives the exact selection before recording its ADMIT and publishing
the generation. Its CONTROL journal now binds the selection SHA256 as well as
the native bundle and source deadline. Every existing native observation already
carries that selection digest and admission sequence. The hot callback performs
no additional file/network I/O, JSON parsing or receipt validation.

## Producer and source policy

The existing hotset-selection process gains public venue collection; no new
process or authority is added. Launcher and process manifest pass
`--collect-venue-terms --venue-terms-directory .../graph_hotset/native_venue_terms`.
Collection runs on its existing COLLECTOR CPU class, with at most four I/O
workers and 64 selected binary market pairs.

Before refresh, the publisher writes `source_valid=false`; old successes are
discarded before any new request. The native observer records invalidation when
it observes the changed publication. Detection is polling-bounded, not claimed
instantaneous. After collection the publisher rechecks current graph/universe
leases. Failed or unsupported receipts remain explicitly UNVERIFIED; they do not
become free-fee or zero-delay terms. Their markets may remain subscribed for book
diagnostics, but cannot pass the required-terms counterfactual gate.

Supported receipts receive a conservative **local cache policy** of at most
30 seconds from the FIRST request's start, not its completion. Native source
expiry is capped accordingly. Refresh is requested after 15 seconds or a target
change. This policy is not a guarantee that the venue cannot change meanwhile.
Source polling/revalidation blackouts remain measurable missing coverage.

The archive is immutable/content-addressed; individual receipts are capped at
8 MiB, the active set at 32 MiB, retained archive at 8 GiB, and collection requires
at least 1 GiB free space. Exceeding a bound fails closed, never deletes evidence.
Actual 8–48 hour archive growth and I/O load still need measurement; these caps
alone do not prove sufficient retention capacity.

## Offline required-terms mode

The native runner accepts `--require-venue-terms`. In that mode it:

1. Opens the exact `<selection_receipt_sha256>.selection.json` in the native
   generation archive, never a mutable current/latest file.
2. Loads referenced sibling `native_venue_terms/<sha256>.json` receipts and
   checks hashes, PAPER boundaries, documented rule identity and request clocks.
3. Recomputes terms from original response bytes, including market/token
   membership and compact/full/book consistency. Re-hashing a fabricated
   `verified` flag or projected hold cannot substitute for that check.
4. Requires collection to precede selection publication and the source lease
   to fit inside each needed receipt's cache window.
5. Matches condition/token identities and fee/minimum/tick operands against the
   exact compiled native relation. Mismatch is censored, not silently overridden.
6. Derives token holds from those receipts. An explicit scenario arm cannot
   replace a documented hold with zero or another conflicting value.
7. Requires the CONTROL ADMIT to bind that exact selection, and checks the
   existing half-open control interval at every matching/unwind book lookup.

An invalidation or expiry between legs preserves already known fills while
censoring later outcomes. A later readmission does not revive the old candidate.
Legacy journals without selection binding cannot satisfy required-terms mode.
Without this flag, scenarios remain explicitly unverified diagnostics; there is
no automatic fallback from failed required provenance into that weaker mode.

Condition/market IDs are retained in the bounded native bundle. No REST bid/ask
prices enter the decision or matching books. Public book responses supply only
the checked venue constraints here; execution continues to use causal WS replay.

Mode, source-module hashes and normalized candidate provenance/admission errors
are included in study identity. Losing a required receipt produces a distinct
censored study instead of silently reusing a prior successful artifact. The
automatic funnel identifies per-candidate admitted holds separately from zero-
hold upper bounds. Both independent and shared-liquidity worlds use this path.

## Validation

- 23 new source-selection/causal integration tests, including future receipts,
  corrupted bytes, recomputed projection hashes, wrong CONTROL selection,
  missing archives, clock/lease expiry, fee operand mismatch, attempted delay
  overrides, between-leg invalidation and REST price non-interference.
- The actual C++ producer's ADMIT selection digest is checked by the Python
  round-trip validator against its emitted native observation.
- Native CONTROL library, loader test, observer and replay executable rebuilt
  in Release, Debug, combined ASan/UBSan and TSan; loader and both cross-language
  integrations pass in all four variants.
- Final full local Python suite: **2,647 passed**, 1 existing skip, 384 existing
  warnings. Final configured Release CTest suite: **412/412 passed**, including
  the report-rendering regression. Unrelated native targets were cached.
- Frozen champion lane, economics and multi-engine have no diff; no test was
  weakened. Existing duplicate-library linker warning remains.

These are local gates, not clean official Linux release/security CI, independent
venue authentication, London performance measurements or economic evidence.

## Remaining requirements

Receipt integrity and recorded local eligibility do **not** verify atomic source
snapshots, matching-time immutability, fee fragmentation/collection, sports-delay
activation, actual ACK/rejection behavior or settlement capital release. All
cycles retain `venue_execution_verified=false` and portfolio economics remain
unknown. Snapshot cache policy must not be mistaken for an exchange guarantee.

Still open: global sizing/champion parity, capital lifecycle/capacity, independent
NegRisk and broader semantic attestations, maker calibration, true side-by-side
load/retention benchmarks, hourly multi-session evidence, clean release gates and
the bounded London PAPER economic study. The latest available SSH probe failed
authentication; no fresh runtime health or deployed SHA is claimed here.

`research_decision = null`. The end-to-end objective is not complete.
