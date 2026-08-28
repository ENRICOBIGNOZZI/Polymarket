# V7 external inputs productionization

Base truth audited on 2026-08-28: `origin/main` and `origin/paper-validated`
both resolved to `53c86714a6745beb6e5fe650fcd4bf9c3a6b8608`.

This implementation extends V7 only. It does not add an OMS, ledger, allocator,
inventory owner, risk plane, execution engine, portfolio state, or monitoring
stack. All external-input components remain RESEARCH and PAPER-only.

## Provider decisions

| Capability | Selection | Transport | Credential | Current truthful state |
|---|---|---|---|---|
| Sports primary | Sportradar Soccer v4 Realtime | HTTP chunked push; five-second heartbeat | `PM_V7_SPORTRADAR_API_KEY` | Adapter complete; `CREDENTIALS_REQUIRED` until supplied |
| Sports recovery | Sportradar Soccer v4 timeline REST | Conditional recovery after disconnect/gap | same key | Adapter contract configured; live recovery evidence pending |
| Cross-platform venue | Kalshi | Public REST polling for discovery and orderbooks | none | Collector complete; deployment connectivity validation pending |
| Cross-platform upgrade | Kalshi WebSocket v2 | authenticated WebSocket snapshot + delta | key id/private key | Optional; not used or claimed without credentials |

Primary documentation:

- Sportradar push behavior, credentials, redirects, chunking, heartbeats and
  REST recovery: <https://developer.sportradar.com/soccer/docs/soccer-ig-push>
- Sportradar live timelines and play-by-play semantics:
  <https://developer.sportradar.com/soccer/docs/soccer-ig-live-match-retrieval>
- Kalshi public market-data access:
  <https://docs.kalshi.com/getting_started/quick_start_market_data>
- Kalshi orderbook semantics:
  <https://docs.kalshi.com/api-reference/market/get-market-orderbook>
- Kalshi WebSocket orderbook snapshot/delta behavior:
  <https://docs.kalshi.com/websockets/orderbook-updates>

No endpoint is called official merely because its hostname or payload looks
plausible. The provider registry records the authoritative documentation URL,
transport, credential contract and known semantics.

## Canonical flow

External bytes enter a component-specific append-only causal tape. Normalized
events then pass through `v7_semantic_mapping.py`. Candidate generation is
non-authoritative. Only an unexpired `VERIFIED` mapping whose complete semantic
field hashes match its independent evidence bundle can enter an existing
strategy kernel. Research outputs have no capital, OMS, ledger-write, execution,
or promotion authority.

## Semantic verification

The shared fingerprint compares event definition, entities, event type,
jurisdiction, geography, outcomes, direction, threshold, comparison operator,
measurement window, deadline, timezone, resolution source and rules,
cancellation, void, postponement, exceptions, and settlement currency.

Only `EXACT_EQUIVALENT` and `COMPLEMENT_EQUIVALENT` are actionable by the
research kernels. Title, keyword, embedding or LLM similarity can create a
`CANDIDATE` only. Any contract-rule or source-hash change expires the mapping.

The checked-in mapping registry deliberately starts empty. No pair found during
this implementation had a persisted, independently attested complete settlement
bundle. This is an explicit evidence blocker, not missing code and not a reason
to invent a mapping.

## Timing truth

Every tape distinguishes UTC wall time from local monotonic receive time.
Sportradar provider event time is retained separately from receive time. Kalshi
REST observations are labelled `PUBLIC_REST_POLLING` and
`polling_latency_not_event_latency=true`; HTTP request time is never called
exchange event latency. Sub-millisecond claims are forbidden until supported by
source-resolution and measured forward evidence.

## Failure isolation

- Missing Sportradar credentials produces `CREDENTIALS_REQUIRED` and the sports
  worker remains alive.
- Kalshi transport failure disables only cross-platform research.
- Missing or expired mappings disable only the affected external sleeve.
- Parser failures, stale feed, unresolved gaps, contract changes and unsafe
  authority fields fail closed locally.
- Maker, structural, graph, canonical OMS, allocator, risk and ledger continue.

## Status dimensions

Implementation, feed, mapping, forward collection, engineering validation and
economic evidence are separate fields. `IMPLEMENTATION_COMPLETE` never implies
`FEED_OPERATIONAL`, `MAPPING_VERIFIED`, or `ECONOMICALLY_VALIDATED`.

At source time the truthful expected blockers are:

- OSINT: authoritative feed code and causal tape complete; no verified
  event-to-contract mapping.
- Sports: adapter complete; Sportradar Realtime credential and verified mapping
  required.
- Cross-platform: public Kalshi collector complete; exact/complement mapping and
  authoritative fee evidence required.

Runtime and server health must replace these expectations with measured status
from the deployed exact SHA.
