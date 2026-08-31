# Real PnL verification

`scripts/v7_execution_ledger.py` remains the single canonical ledger writer.
It now accepts hash-chained `ECONOMIC_JOURNAL` records with balanced postings in
integer pUSD or outcome-token base units.  A journal fact never submits an
order; it records an independently observed deposit, fill, fee, rebate, gas,
split/merge/redeem, or settlement fact.

Collectors place unsealed journal facts in the existing atomic spool through
`spool_journal_entry`; `v7_ledger_spool.py` is the only component that links
and seals them in `ledger/execution.jsonl`.

`scripts/v7_real_pnl_verifier.py` is deliberately read-only and independent of
the production ledger module.  It verifies journal hashes and posting balance,
requires hash-chained raw CLOB/Data API/wallet/Polygon evidence, reconciled
wallet balances, rejects open outcome inventory, and
reconstructs terminal PnL (pUSD plus any final USDC.e collateral) as:

`terminal pUSD cash + terminal USDC.e cash + equity:external_funding pUSD`

It returns `REAL_PNL_RECONCILED_UNSIGNED` only after all required independent
sources are represented. `REAL_PNL_VERIFIED` additionally requires a private
runtime HMAC attestation. Neither PAPER events, virtual fills, quoted edge, nor
an empty reconciliation can reach either state.

`scripts/v7_execution_provenance.py` adds a separate immutable chain for
`DECISION → SIGNED_ORDER → CLOB_ACCEPTED → FILL → SETTLEMENT`. It stores only
payload and signature digests—never a private key or signature—and links its
CLOB and settlement stages to raw read-only evidence. The independent verifier
requires a completed chain and a matching provenance link for each CLOB journal
fact before it can report reconciled real PnL. A FILL lineage must also equal
the raw trade's `takerOrderId`, preventing an otherwise complete provenance
chain for one order from being reused to justify another order's fill. A
`CLOB_ACCEPTED` stage must instead link to the immutable V2 user-WebSocket
`order` `PLACEMENT` event for that same lineage; a REST summary, update, or
cancel event cannot impersonate acceptance. A FILL must link to the matching
settled V2 user-WebSocket `trade` event. These checks keep acknowledgment and
execution facts separate and independently reproducible.
lineage supports one or more sequential FILL records after acceptance and
before settlement, so partial fills retain separate raw-evidence links rather
than being collapsed or silently overwritten.

`scripts/v7_real_pnl_scorecard.py` consumes only a `REAL_PNL_VERIFIED` report
and a separate hash-chained terminal-unit tape. It computes event-clustered
lower confidence bounds at 1x/1.5x/2x costs, reward-free PnL, capacity tiers,
drawdown, and expected shortfall. Missing regimes, capacity, samples, a report
reconciliation mismatch, or any non-positive 2x lower bound returns
`MORE_EVIDENCE_REQUIRED`; it never automatically promotes a strategy or makes
a world-class claim.

## Pre-canary security gate

`scripts/v7_secret_scan.py` scans tracked files and reachable git blobs without
printing matched values. A fresh full-history scan currently identifies a
historical credential-like assignment, so `v7_live_capability.py` blocks a
future authenticated canary. The credential must be rotated and the incident
remediated outside this repository; only then can a clean, redacted full-history
scan satisfy the gate. Checked-in configuration cannot override it.

The live-capability CLI is deliberately minimal: it validates only the
PAPER-only zero-limit configuration and a fresh history scan, then emits a
three-field security summary. It has no signing key,
identity, exact-SHA binding, or order-submission capability.

## CLOB V2 authentication wire contract

`scripts/v7_clob_v2_auth.py` implements the exact request and user-stream
authentication shapes without opening a connection: an external signer provides
the L1 EIP-712 signature for `/auth/api-key`; L2 uses URL-safe Base64 HMAC over
`timestamp + method + query-free path + exact body`; the user-stream frame is
the documented `type: user` frame. The module does not serialize a body after
signing, read environment variables, persist credentials, or submit orders.

`scripts/v7_clob_v2_order_wire.py` supplies the missing order side of that
contract. It builds the V2 Exchange `Order` typed data for the standard or
negative-risk exchange, including the documented ERC-7739 `TypedDataSign`
wrapper required by Deposit Wallets. It validates all integer and wallet
fields, serializes the documented `/order` body exactly once, and can pass
precisely those bytes to the L2 builder. It neither invokes a wallet signer nor
performs network I/O. This prevents a body from being changed between L2
signing and a future explicitly-authorized transport call.

`clob_user_ws_record` in `scripts/v7_real_pnl_evidence.py` converts one
captured authenticated user-stream order/trade message into an unsealed evidence
record. It stores the original UTF-8 JSON and its byte hash before the exclusive
tape writer seals it, accepts only the current V2 `topic: user` / `type` /
`payload` order and trade frames, rejects subscription/heartbeat frames and
duplicate JSON keys, and therefore gives fills a direct immutable input to
reconciliation.

`scripts/v7_clob_v2_journal.py` then derives a balanced `TRADE_FILL` entry
directly from a sealed trade event: pUSD cash and ERC-1155 outcome units are
converted with `Decimal` to exact six-decimal base units, with explicit CLOB
clearing legs. It requires the matching provenance FILL hash and does not append
or transmit anything itself. Only the final `CONFIRMED` trade state is
eligible; matched, mined, failed, or retrying events cannot become accounting
facts. The
independent verifier reparses the V2 raw frame, checks every trade identifier,
price, size and side against journal metadata, and recomputes all four cash and
token postings before accepting the fill. The user-trade collector preserves
`traderSide` and `feeRateBps`; a non-zero taker rate is deliberately rejected
until a separate observed fee fact can be linked. This is fail-closed: a rate
is not treated as a guessed cash fee, and a gross fill can never be presented
as net PnL.

The economic journal also records maker and taker rebates as distinct entry
types, so reward-free and rebate-aware PnL can be reconstructed separately.

`scripts/v7_data_api_activity_journal.py` maps sealed Data API activity pages
to exact pUSD facts for deposits, withdrawals, liquidity rewards, maker rebates,
and taker rebates. Ambiguous types such as split/merge/redeem remain evidence
only until independently decoded from the corresponding Polygon transaction.
Every conversion adapter revalidates the sealed evidence hash before deriving a
journal entry; a lookalike object or a mutation after sealing is rejected.

`scripts/v7_polygon_lifecycle_journal.py` provides that receipt decoder for
split, merge, and redeem. It decodes pUSD ERC-20 plus ERC-1155
`TransferSingle`/`TransferBatch` logs from a sealed Polygon RPC response,
checks the lifecycle invariant in integer base units, and produces the balanced
cash/token clearing legs. The decoder requires the exact Polygon-137
`eth_getTransactionReceipt` request for the receipt transaction. The independent
verifier repeats the redeem decoding from the raw receipt and requires it for the `SETTLEMENT` provenance stage; a
Data API activity row provides coverage, but cannot impersonate settlement. No
RPC request or transaction is made by either module.
For a final redemption, the optional immutable provenance terminal hash is
carried into the journal entry; the independent verifier requires every
completed decision-to-settlement lineage to have this exact journal/evidence
link.

`scripts/v7_pusd_conversion_journal.py` likewise decodes sealed Polygon
receipts for the official CollateralOnramp and CollateralOfframp. It requires
equal and opposite USDC.e/pUSD ERC-20 wallet deltas before recording a
`PUSD_WRAP` or `PUSD_UNWRAP` fact; mismatched, partial, or wrong-ramp receipts
are rejected.

`scripts/v7_wallet_balance_snapshot.py` converts only sealed `eth_call`
responses pinned to the same Polygon block hash into the verifier's observed
wallet-balance map. It independently checks the pUSD and Conditional Tokens
contract addresses and exact ERC-20/ERC-1155 `balanceOf` calldata. This keeps a
wallet reconciliation from comparing balances observed at different chain
states; it is a decoder, not a wallet or RPC client.
The optional CLI writes both this traceable snapshot and the flat balance map
consumed by the independent verifier. Only the traceable snapshot can support
`REAL_PNL_RECONCILED_UNSIGNED`: the verifier independently rechecks its RPC
call identity, shared block hash, values, and evidence-record hashes. A flat
map remains useful for diagnostics but cannot attest a real balance.

`scripts/v7_data_api_position_snapshot.py` converts one sealed `/positions`
page into exact six-decimal outcome-token balances. The verifier independently
reparses that page and requires it to match the terminal ledger inventory, in
addition to the direct wallet snapshot.

`scripts/v7_data_api_activity_coverage.py` requires a contiguous `/activity`
page sequence from offset zero through a short terminal page, explicitly with
deposits and withdrawals included. The verifier reparses that manifest before
granting a reconciled PnL state, preventing a selectively collected Activity
page from standing in for full cash-flow coverage.

## CLOB V2 incident controls

`ClobV2RecoveryGate` is a pure, fail-closed OMS guard for observed matching
engine restrictions. It starts requiring reconciliation; an HTTP 425 moves it
to bounded exponential backoff, followed by a 120-second post-only window only
after a healthy engine observation. A cancel-only incident always preserves
cancel admission but blocks all new orders. The liveness plane maps those
states to `NoNewQuotes`, `Reconcile`, or post-only maker admission, so a venue
recovery cannot silently restore ordinary quoting.

`OrderHeartbeatGate` drives the separate CLOB order-heartbeat requirement: a
heartbeat is due every five seconds, a valid acknowledgement must arrive within
ten seconds of activation or the last valid acknowledgement, and an expiry is
terminal until the account is cancelled and reconciled. It rejects acknowledgements
without a preceding wire-send or with non-monotonic timing.

`SignerRateLimiter` independently budgets each signer in a one-second rolling
window. Ten regular submissions and ten reserved cancel/heartbeat requests are
permitted; a clock regression quarantines only the affected signer and requires
reconciliation. This control is deterministic and is not a signer or network
client.

The checked-in supervision contract pins those timings and remains PAPER-only;
it cannot grant authentication, real-order submission, or capital authority.
