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
