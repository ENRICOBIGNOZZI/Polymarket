# Aggressive V5 activity redesign

## Observed failure mode

The current V5 public-data smoke can report a healthy runtime while producing little economic activity. On the latest exact-main snapshot (`079c5ce498f9e26fd17bb7d513a6e892b20fb4cd`), B1 discovered 240 markets, retained 34 usable history series and tested 561 pairs, but fit zero models. B2 discovered 240 markets, built 31 panel series, passed 28 residuals through the mean-reversion gate and 11 through the relation gate, but only one hedge survived and only one row had positive raw expected edge.

That surviving B2 row is not close to executable profitability. Its raw expected edge is about **+0.42 bps**, while maker-entry net edge is about **-17.69 bps** and taker net edge about **-51.57 bps**. The implied raw-to-maker execution wedge is about **18.11 bps**, over 43 times the observed raw edge. The trade recorder was healthy in the same snapshot, so this is not explained by a stale public tape.

Walk-forward remains empty: 0 OOS trades, 0 active folds, $0 realized OOS net PnL and no tiny-pilot eligibility.

## Alpha Factory decision

**REJECT_THRESHOLD_RELAXATION_CURRENT_SAMPLE.**

The latest raw-positive candidate is already negative after executable maker and taker costs. Lowering `min_net_edge`, z-score, sample, correlation, stability or reversion admission thresholds cannot by itself change that post-cost sign. Treating more admitted raw candidates as alpha would therefore optimize activity rather than expected net PnL.

This does not reject wider V5 coverage. It changes the order of experiments: first expand measurement and model coverage while holding economic execution gates fixed; only then test whether any threshold relaxation admits candidates that are still positive after spread, fees, slippage, queue/fill, latency, adverse selection and uncertainty.

## Bounded challenger

The branch now includes `scripts/v5_activity_frontier.py` and a deterministic regression test. The diagnostic separates three failure classes:

1. **search/model coverage bound** — too few usable series, models, relation-compatible hedges or model outputs;
2. **execution-cost bound** — raw edge exists but maker/taker executable edge is non-positive;
3. **post-cost-positive but threshold-blocked** — the only class in which lowering an admission threshold can be economically relevant.

The current sample falls in class 2 for the only raw-positive B2 row and has no B1 row at all.

## Experiment order

1. Run micro, PCA, graph, semantic and external sleeves continuously and exercise all five in public smoke, publishing a per-cycle funnel for discovered -> model-eligible -> raw-positive -> maker/taker-positive -> admitted -> filled -> realized-PnL states.
2. Expand the research universe toward 500 markets and lower discovery-only liquidity filters while leaving all post-cost execution gates unchanged.
3. Test model-specific changes separately on identical chronological rows: LF factor/relation/reversion work remains owned by the LF research queue; queue/fill/latency/markout work remains owned by HF research.
4. Consider lower economic thresholds only if a common-sample ablation finds rows that are genuinely positive after executable costs but blocked solely by the incumbent threshold.
5. Compare every activity challenger against incumbent V5 on identical future windows and require positive incremental realized net paper PnL at normal, 1.5x and 2.0x execution costs.

## Promotion and rollback criteria

No activity redesign is evidence-ready until it has enough independent OOS executions/folds to satisfy the existing statistical gates and positive incremental utility versus incumbent V5. Reject or roll back a challenger if incremental realized net PnL is non-positive, if either 1.5x or 2.0x stressed incremental PnL is non-positive, if its result depends on future/post-decision information, or if it needs weaker drawdown, concentration, OOS, kill-switch or execution safeguards.

## Risk invariants

- Paper-only execution remains mandatory.
- Every submitted paper order must retain positive modeled net edge after protocol fees, slippage and configured uncertainty penalty.
- Global and sleeve drawdown kill switches remain active at 15%.
- Per-market, per-event, gross-exposure, cash and order-book depth limits remain active.
- Structural arbitrage and multileg execution continue to use executable prices rather than mid-price opportunities.
- `config/live_champion.json`, credentials, authenticated execution and real-money trading remain unchanged.
