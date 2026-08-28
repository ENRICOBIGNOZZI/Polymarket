# V7 Execution

All strategy output is normalized to account-wealth candidate actions. Priority is global kill, strategy/market kill, critical cancel, liquidation, urgent inventory reduction, normal risk reduction, structural arbitrage, informed take, passive make and nothing.

Alpha actions require positive robust expected wealth after fees, depth, slippage, latency, capital time, execution uncertainty and inventory/unwind risk. Risk actions do not require positive alpha.

Maker execution models queue uncertainty, fillability and fill-conditioned markout. Multi-leg execution models direct joint states and sequential partial/unwind outcomes; products of marginal fill probabilities are not accepted as the canonical completion model.

Self-cross prevention is cancel, canonical-state confirmation, then submit. Emergency cancel capacity is reserved from normal quoting.
