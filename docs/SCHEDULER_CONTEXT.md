# Shared scheduler context

Every autonomous Polymarket scheduler reads the same machine-readable contract:

```text
config/scheduler_context.json
```

The contract prevents a research job, merge job, validation job, deployment job or health job from silently adopting a different model of the system.

## Remote runtime

The supported remote path is:

```text
GitHub Actions -> Tailscale -> SSH
```

The canonical target is supplied through repository variables, with the existing defaults:

```text
host: 100.104.183.109
user: enrico
port: 22
repository: $HOME/polymarket
```

Authentication remains in GitHub secrets:

```text
TS_AUTHKEY
POLYMARKET_SERVER_SSH_KEY
```

No scheduler may print, persist or copy either secret into an artifact. Deploy and health jobs must use batch-mode SSH, keepalive settings and the exact `paper-validated` revision. The explicit live selector remains:

```text
config/live_champion.json
```

A remote job fails closed when the tailnet is unavailable, the SSH identity is missing, the repository cannot be reached, a SHA relation is inconsistent, or a required service is stale.

## Quantitative architecture

For market `i` at time `t`, the universal state is organized as

```text
Z_i,t = (market microstructure,
         contract characteristics,
         cross-market information,
         external information).
```

The five complementary engines are:

1. microstructure fair value;
2. PCA and statistical arbitrage;
3. event graph and logical consistency;
4. semantic relative value;
5. external information.

They feed an adaptive mixture of experts that emits a fair probability and an uncertainty estimate. A scheduler must preserve the distinction

```text
probability estimation
!= trade decision
!= portfolio construction and risk
!= execution and reconciliation.
```

The common decision object is executable net edge:

```text
net edge
= fair side probability
- executable bid or ask
- fees
- slippage
- uncertainty penalty.
```

When the bid or ask is already used as the executable price, the spread is not subtracted a second time.

## Portfolio and risk

Every production-facing proposal must preserve:

- fractional Kelly rather than unrestricted Kelly;
- gross-exposure limits;
- market-level concentration limits;
- event-level concentration limits;
- an ex-ante open-loss budget;
- a 15% operating drawdown kill threshold.

The drawdown threshold is an operating control, not a mathematical guarantee against gaps, resolution shocks, stale data or software failure.

## Research and execution boundary

The research engine may consume public live data, estimate fair values, generate statistical or structural candidates, simulate fills, calculate paper PnL and write diagnostics.

It may not infer authorization for authenticated order submission. Real-money execution remains a separate approval, credential, reconciliation and kill-switch boundary.

The fast-arbitrage and maker schedulers therefore remain shadow or paper until chronological, executable-cost, latency, fill, adverse-selection and OOS evidence has passed the integration and post-merge validation chain.

## Scheduler profiles

The shared contract assigns every registered scheduler one profile:

- `supervisor`: observe the whole control plane without production mutation;
- `policy`: enforce branch, context, isolation and approval rules;
- `research`: generate evidence and research-only candidates;
- `integration`: merge at most one fully approved unified champion change;
- `validation`: bind tests to one exact revision;
- `remote`: deploy or inspect `paper-validated` through Tailscale and SSH;
- `api`: check read-only public connectivity.

Every run records the SHA256 of `config/scheduler_context.json`. A context change is therefore visible in scheduler artifacts and cannot be confused with an unchanged research or deployment regime.
