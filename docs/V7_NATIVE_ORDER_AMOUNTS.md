# V7 Native Exchange V2 Order Amounts

The official V2 limit-order builder rounds size down to two decimals and applies
tick-specific price/amount precision. The native crypto path already carries an
integer canonical price (`e4`) and integer share quantity (`1e6`).

For every current CLOB tick (`0.1`, `0.01`, `0.005`, `0.0025`, `0.001`,
`0.0001`), a tick-aligned price multiplied by a two-decimal share size has no
more decimal places than the official amount precision. The marketable-limit
FAK/FOK path can therefore reproduce the SDK with exact integer arithmetic:

- round shares down to 0.01;
- micro-USDC notional = rounded share micro-units x `price_e4` / 10,000;
- BUY: makerAmount = USDC notional, takerAmount = shares;
- SELL: makerAmount = shares, takerAmount = USDC notional.

The function rejects unsupported ticks, misaligned prices, sub-0.01 quantities,
invalid sides and integer overflow. It does not select price, size, order type,
fees, signing or authority.
