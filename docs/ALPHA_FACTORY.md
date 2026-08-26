# V7 Alpha Factory

Alpha Factory is a PAPER-only research/evidence component. It evaluates V7 challengers and produces durable evidence; it does not merge, deploy, move `paper-validated`, mutate the live champion or submit authenticated orders.

## Inputs

The factory consumes persisted, auditable evidence such as:

- canonical V7 runtime/action reports;
- prospective maker queue/fill/markout observations;
- finalized execution ledger outcomes;
- chronological research outputs for PCA, Local Factor and ranking;
- exact-code provenance and data-health metadata.

Evidence from incompatible code revisions or test windows is not pooled.

## Evaluation

Depending on the candidate, Alpha Factory evaluates:

1. executable OOS PnL after verified fees, slippage and depth;
2. 1.5x and 2.0x cost stress where the evidence supports that decomposition;
3. drawdown and profit factor;
4. chronological fold stability;
5. block/bootstrap evidence and FDR control where statistically applicable;
6. data health and evidence completeness;
7. incremental utility relative to the canonical system;
8. compatibility with one V7 runtime owner, ledger and broker authority.

For maker research, fill count alone is not an objective: fill-conditioned PnL and adverse markout remain central. For multi-leg strategies, evidence must reflect joint completion and partial/unwind states rather than products of marginal fill probabilities.

## Output

The workflow may publish research artifacts and a prioritized next-experiment queue. A recommendation is not a promotion authorization.

The promotion path is:

```text
V7 research evidence
 -> exact-head governance verdict
 -> integration/* candidate
 -> Promotion Controller
 -> Integration Merge
 -> exact-SHA validation
 -> paper-validated
 -> deployment
 -> server-health
```

The Promotion Controller independently checks its configured objective gates and exact source/content provenance before issuing the ephemeral `autonomous-promotion-approved` label.

## Valid outcomes

All of the following are legitimate outcomes:

```text
REJECTED
MORE_EVIDENCE_REQUIRED
SHADOW_ONLY
APPROVED_FOR_INTEGRATION
INTEGRATION_READY
```

Zero candidates or zero trades are not reasons to weaken economics automatically.

## Failure behavior

The research plane fails closed:

- stale/missing evidence blocks promotion;
- negative stressed economics remains negative evidence;
- drawdown or data-health violations are surfaced, not hidden;
- source/head drift invalidates exact-code evidence;
- a challenger cannot redefine operator authority;
- authenticated real-money execution remains disabled.

Durable summaries may be published on the telemetry branch and as GitHub Actions artifacts, but telemetry is evidence, not a second live configuration.
