# V7 Replay

Replay consumes receive-time ordered inputs for the canonical Crypto Settlement Engine. It rejects future information, mixed-SHA evidence and incomplete cost vectors. Maker, taker, cancel and settlement component results reconcile into the one crypto engine identity and the single canonical portfolio.

Missing observations remain missing; replay must not convert unavailable crypto evidence into synthetic zeroes.
