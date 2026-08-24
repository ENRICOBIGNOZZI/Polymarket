# Shared scheduler context

Every autonomous Polymarket scheduler reads the same machine-readable contract:

```text
config/scheduler_context.json
```

The contract prevents research, merge, validation, deployment and health jobs from silently adopting different assumptions.

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

No scheduler may print or persist either secret. Deploy and health jobs use batch-mode SSH, keepalives and the exact `paper-validated` revision. The explicit live selector remains `config/live_champion.json`.

## Quantitative architecture

The universal state is

```text
Z_i,t = (market microstructure,
         contract characteristics,
         cross-market information,
         external information).
```

The five complementary engines are microstructure fair value, PCA/statistical arbitrage, event-graph/logical consistency, semantic relative value and external information. They feed an adaptive mixture of experts that emits a fair probability and uncertainty.

Every scheduler preserves

```text
probability estimation
!= trade decision
!= portfolio construction and risk
!= execution and reconciliation.
```

The common decision object is executable net edge:

```text
fair side probability
- executable bid or ask
- fees
- slippage
- uncertainty penalty.
```

When the bid or ask is already used, spread is not subtracted a second time.

## Portfolio and risk

Production-facing proposals preserve fractional Kelly, gross and concentration limits, an ex-ante open-loss budget and a 15% operating drawdown kill threshold. That threshold is not a mathematical guarantee against gaps, resolution shocks, stale data or software failure.

## Research and execution boundary

The research engine may consume public live data, estimate fair values, generate candidates, simulate fills, calculate paper PnL and write diagnostics. It may not infer authorization for authenticated order submission. Real-money execution remains a separate approval, credential, reconciliation and kill-switch boundary.

## Scheduler profiles

- `supervisor`: observe the control plane without production mutation;
- `policy`: enforce lifecycle, context, isolation and approval rules;
- `research`: generate evidence and research-only candidates;
- `integration`: merge at most one fully approved unified champion change;
- `validation`: bind tests to one exact revision;
- `remote`: deploy or inspect `paper-validated` through Tailscale and SSH;
- `api`: check read-only public connectivity.

Every run records the SHA256 of `config/scheduler_context.json`, so evidence generated under different system assumptions cannot be silently pooled.
