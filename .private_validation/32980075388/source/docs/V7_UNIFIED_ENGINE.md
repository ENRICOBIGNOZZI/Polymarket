# Unified V7 PAPER engine

Canonical structure before integration to `main`:

- `scripts/paper_v7_loop.sh`: one top-level PAPER supervisor.
- `scripts/paper_v7_execution_loop.sh`: one execution owner for Maker, Micro Taker, Graph/RV, Hard Arb and External Intelligence.
- `scripts/v7_shadow_loop.py`: multi-frequency forward research for PCA, Local Factor and cross-sectional ranking.
- `scripts/v7_multileg_broker_runner.py`: dual-clock, shared-capacity, canonical-event multi-leg broker with 100% economic completion and `SETTLING` semantics.
- `scripts/v7_micro_maker_worker.py`: fill-conditioned toxicity-aware maker with dual-clock fills and cross-sleeve token ownership.
- `scripts/v7_micro_taker_worker.py`: fixed-horizon complete round-trip executable-EV taker.
- `scripts/v7_graph_forward_guard.py`: prospective point-in-time joint-state Graph/RV evidence; no current-book historical replay.
- `scripts/v7_bundle_quote_optimizer.py`: per-leg quote priority only; product of marginal fills is forbidden as a joint estimator.
- `config/v7_frequency_matrix.json`: HF cadence tests plus 30m/1h/2h/6h forecast horizons with separate OOS evidence.

The incumbent `config/live_champion.json` remains V6 on the research branch. `config/v7_champion_candidate.json` is the candidate descriptor. Switching the canonical champion belongs to the exact-head integration/promotion step, not to the research branch itself.

Cleanup is intentionally deferred until the V7 structure is present on `main`. Only then should V3/V4/V5 and superseded V6 adapters, research branches and duplicate workflows be removed.
