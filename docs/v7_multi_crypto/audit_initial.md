# V7 Multi-Crypto Lead/Lag — Initial Audit

Observed: 2026-09-16. Scope: read-only audit of the active runtime plus isolated development worktree.

## Runtime identity

- Host: `Mac.usilu.net`, Darwin arm64.
- Active checkout: `/Users/enrico/polymarket`.
- Active code SHA: `940be03872027e3219a2bf1db5e59263d66110a9`.
- Active checkout is detached HEAD and was clean at audit start.
- Frozen BTC protocol SHA-256: `8b463ef8f66cd07fa694cbb49e355b812eac8d4724c35f65bd3e6a3505719d96`.
- Active runtime reports PAPER only: `paper_only=true`, `authenticated_execution=false`,
  `real_order_submission=false`, `real_capital_at_risk=false`.

## Frozen BTC forward snapshot

At audit time the lead/lag state reported 5 PAPER entries, 4 settled, 4 wins,
1 open position, and PAPER realized PnL 10.533245. These are simulator/settlement
observations. They are not evidence of real matching-engine execution or real-money profit.

## Concrete implementation findings

1. `LeadLagRuntime.books()` calls `ClobBooksClient.request_books()` synchronously.
2. `candidate_step()` reads the CLOB before proposal and again after coordinator authorization.
3. `wait_receipt()` polls a receipt file and sleeps 5 ms between reads.
4. PAPER `FILL` uses `recorded_ts_ms = decision_ms + 1`; this is synthetic ordering, not observed fill latency.
5. Persistent C++ external venue WebSockets already exist for Binance, Coinbase, Bybit,
   Binance USD-M, Bybit linear and Deribit, but the connection factory/runtime is BTC-hardcoded.
6. The existing settlement registry contains BTC/ETH/SOL/XRP M5/M15 contexts.
   DOGE and BNB are absent and therefore are not treated as verified.
7. ETH and SOL have existing zero-authority shadow contexts that can be reused without granting entry authority.

## Isolation decision

The active BTC checkout/processes were not edited or restarted.
Development is isolated in:

- worktree: `/Users/enrico/polymarket-multi-crypto-v7`
- branch: `research/v7-multi-crypto-lead-lag-20260916`
- base SHA: `940be03872027e3219a2bf1db5e59263d66110a9`

No code from this worktree is deployed.

## Initial priorities

1. Parameterize the existing venue data plane instead of copying one runtime per asset.
2. Build a fail-closed six-asset capability registry.
3. Reuse the existing PM WebSocket/causal-book infrastructure for the new shadow path.
4. Remove REST and receipt-file polling only in the new path; preserve frozen BTC behavior.
5. Keep one coordinator, one execution owner and one canonical ledger writer.
## Isolated implementation progress after the audit

- The external venue runtime is now parameterized for six assets in the isolated worktree; the active BTC runtime remains untouched.
- DOGE and BNB M5/M15 settlement contexts were added only after live Gamma rules verified Chainlink 60 s TWAP semantics and explicit token mappings.
- The canonical model/source registries now index all six assets with new-risk authority still false.
- DOGE and BNB public venue smokes were observed; unsupported optional venues can now be disabled explicitly instead of being replaced by BTC defaults.
- A transport-freshness mode was added only for new lanes so an unchanged book is not declared stale solely because its price did not move. Frozen BTC retains the previous semantics.
