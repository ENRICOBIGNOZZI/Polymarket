# Graph research and taker exploration

`GRAPH_RV` is active as a paper research scanner, not as a broker route. Its
former scanner spread is a conditional payoff proxy, not a 2% executable edge.
For every multi-leg candidate, `graph_research_ev.py` estimates one empirical
joint distribution for complete, partial and zero basket fills from finalized
paper ledgers with the same leg count. It then reports:

`P(full) * (conditional alpha - costs - adverse markout) - P(partial) * unwind loss - capital/latency cost`.

It rejects products of marginal leg-fill probabilities. Before the configured
number of finalized joint observations, candidates are explicitly
`RESEARCH_INSUFFICIENT_EVIDENCE`. Even a positive research estimate remains
outside `intents.csv`; Graph cannot be routed by this path.

The micro taker has a separate paper exploration sleeve inside its existing
capital sleeve. It is capped by the V6 manifest at $5 per entry, two concurrent
positions and six opens per hour, with a fixed 45-second exit. A taker has no
queue-ahead position, so it records displayed best-ask depth as a queue-pressure
proxy and stratifies each entry by side, 60-second public trade activity and
that depth bucket. Entries and exits use a depth walk, market fee descriptor (or
a conservative fallback), latency stress and partial-exit accounting. Markouts
are recorded at 1, 10 and 45 seconds.

Exploration rows are deliberately excluded from `v7_execution_evidence.py`'s
alpha-promotion sample. Their purpose is to identify a subset worth testing,
not to turn a small diagnostic PnL into proof of alpha or real-money authority.
