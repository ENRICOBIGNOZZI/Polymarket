# V6 model-specific paper architecture

V6 removes ensemble averaging from the operational decision path and assigns each alpha source an economically coherent task:

- microstructure is split into maker fill/adverse-selection and short-horizon taker forecasting;
- statistical arbitrage is estimated on local event, entity and payoff-family clusters rather than one heterogeneous global panel;
- graph logic produces executable multi-leg hard-arbitrage candidates only from verified complete NegRisk sets;
- structural threshold relations generate monotonicity and implication trades;
- semantic parsing discovers entities, deadlines, operators and payoff relations, but does not directly manufacture fair values;
- external signals remain independent terminal-probability forecasts with freshness and provenance.

All execution in this revision is paper-only. Shared portfolio controls retain the global drawdown budget and concentration limits. Scheduler research and promotion must evaluate model coverage, executable-edge conversion, fills, edge realization, cost stress and out-of-sample stability separately for each model family.
