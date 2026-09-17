# V7 Event-Driven Lead-Lag V2 Shadow

The frozen BTC M5 V1 research rule evaluates a 100ms Binance shock on a 25ms
receive-time grid. That grid is part of V1 semantics and is not changed.

This module is a distinct zero-authority V2 shadow primitive. It evaluates on
every causally ordered Binance trade and uses the latest Coinbase mid available
at that exact local receive time. Therefore its trigger timestamp is the actual
causal Binance receive timestamp, not the next grid point.

The economic constants remain intentionally familiar for comparison: 100ms shock
window, 0.30bp Binance threshold, non-opposing Coinbase confirmation, 250ms
cooldown, 300ms warmup and 100ms signal TTL. Removing the grid changes strategy
semantics, so V2 requires its own prospective research identity before any PAPER
promotion.

Implementation properties:
- one owner, globally receive-time-ordered events;
- fixed 8,192-sample rings for Binance and Coinbase;
- amortized O(1) history maintenance: each sample is inserted and retired once;
- no heap allocation, file I/O, REST, locks, sleep or polling;
- fail closed on invalid/out-of-order input or bounded-history overflow;
- explicit causal/prior timestamps carried with every signal.

This file and module do not modify the current V1 runtime or execution authority.
