# NegRisk independent observation checkpoint

Branch `research/unified-exact-arb-graph`; base
`f50147e18e02ebed76cabe900b708e4d9479e603` plus local changes. PAPER only;
no new executable relation, deployment, authority or profitability claim.

`v7_exact_arb_negrisk_attestation.py` enumerates all adapter question indices
at a finalized, hash-pinned Polygon block, checks wrapper/adapter/CTF bindings,
records code hashes and CTF payout state, and binds Gamma metadata by actual
condition/YES/NO token identities. Missing metadata members are retained, never
dropped to manufacture a partition. Two providers must agree for a corroborated
historical receipt. Neither agreement nor a hash constitutes a state proof or
deployed-bytecode/source verification.

The collector is strictly public/read-only, bounded by call/byte/time budgets,
with no credentials, transactions, signing, TLS bypass or provider-limit bypass.
Offline transcript replay rejects fabricated projections even if rehashed.
Conversion resource vectors are formula previews, with unknown gas/readiness/
latency, not enabled transformations. There is no executable freshness lease.

## Actual bounded observation

Event 32228, group
`0x7b95a46fc059d27ac3404325fd6280974d96949102201de57b8595f802d7fc00`:
the complete primary RPC transcript establishes an observed five-question group
at block `0x59fe42a`, hash
`0x4be333a3a924a9bdfb2613f7848028fcc72f434dfa528471c686a7d6a947dcc3`.
Gamma covers those five observed conditions; augmentation=false is metadata,
not an independent semantic attestation. Observed adapter fee is zero.

The second provider failed with HTTP 429 on the first collection and HTTP 402
on the paced second collection. Both archived receipts remain failed:

- `evidence/exact-arb-negrisk-20260924/40085eaf714ef8a7425bab3692180488af09625ca6968a8c12bf1204baee79ab.json`
- `evidence/exact-arb-negrisk-20260924/275d15029c60def771d9c9eb4ac599b74c52a88008ef1962563145d86361e2bd.json`

Recovering the complete first-provider trace yields only
`SINGLE_PUBLIC_RPC_STATE_OBSERVATION`; it never upgrades the failed receipt.
No further requests were made to bypass the second provider's refusal.

## Semantic gate fixed

Metadata booleans and equal observed membership lists no longer automatically
create a constant-payout NegRisk basket. Explicit registries/component payloads
cannot bypass this by supplying a rational one-hot matrix or changing the family
label: multi-condition NegRisk relations and NegRisk conversions remain disabled
without independent semantic proof. Same-condition binary complete sets remain
eligible. Mathematical consistency of supplied vectors is not proof that they
describe actual contract outcomes.

Required independent facts still missing include exactly-one terminal outcome,
stable/exhaustive membership, augmented/Other behavior, deployed source binding
and operational conversion terms. The pinned adapter and wrapper sources are
listed in the collector and receipts; source analysis must not be confused with
verification of the deployed runtime. `complete_set_verified=false` and
`conversion_verified=false` throughout. London causal coverage and economic
evidence did not increase from these public metadata observations.

Validation before the fee-audit increment: 97 focused Python tests, 2,697 full
Python tests (one existing skip), and 414 configured Release CTest tests passed.
These are local engineering gates, not deployment or economic certification.
