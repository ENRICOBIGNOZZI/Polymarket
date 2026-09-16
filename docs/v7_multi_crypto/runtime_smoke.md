# V7 Multi-Crypto — Runtime Smoke

Observed: 2026-09-16. Isolated worktree binary only. No deployment and no order authority.

## ETH

- State: `OPERATIONAL`, `valid=true`.
- Safety: PAPER only; authenticated and real-order submission false.
- External cancel: `null` (BTC-only frozen signal is not reused).
- Healthy public feeds during 8 s smoke: Binance spot, Coinbase spot, Bybit spot,
  Bybit linear, Deribit, Binance USD-M depth, Binance USD-M market.
- Frames observed respectively: 122, 95, 72, 246, 24, 63, 15.
- Transport failures observed: 0 on all seven connections.

## SOL

- State: `OPERATIONAL`, `valid=true`.
- External cancel: `null`.
- Same seven public feed classes connected and healthy during 8 s smoke.
- Frames observed respectively: 138, 105, 56, 274, 11, 62, 28.
- Transport failures observed: 0 on all seven connections.

These smokes validate the parameterized public data plane only. They do not validate
Polymarket contract discovery, PM book routing, signal alpha, fill realism or PnL.
## DOGE

- State: `OPERATIONAL`, `valid=true` in an 8 s isolated smoke.
- Spot books valid: Binance, Coinbase and Bybit.
- Binance USD-M and Bybit linear transports connected and healthy.
- Deribit was explicitly disabled; no fallback data were invented.
- No execution authority and no BTC external-cancel signal.

## BNB

- State: `OPERATIONAL`, `valid=true` after optional-venue support was added.
- Spot books valid: Binance and Bybit; `fresh_venue_count=2`.
- Coinbase and Deribit were explicitly disabled because they are not verified capabilities for this lane.
- Binance USD-M and Bybit linear transports connected and healthy.
- This smoke also verified that an unchanged Bybit book remains usable while the WebSocket transport is fresh; the frozen BTC policy keeps its prior book-age semantics.

## Discovery evidence

A read-only current/next discovery found 24/24 expected markets across BTC/ETH/SOL/XRP/DOGE/BNB and M5/M15. All 24 matched the versioned rule parser and explicit token mapping. The snapshot is stored in `docs/v7_multi_crypto/discovery_observed_20260916.json`. Discovery never grants execution authority.
