# Polymarket V7

Canonical V7 PAPER trading and research system for Polymarket.

There is one operational architecture. V3, V4, V5 and V6 are retired and are not valid runtime, broker, ledger, deploy, monitoring or fallback paths. Git history is the archive.

## Safety

V7 is PAPER-only in this repository:

- `paper_only = true`
- `authenticated_execution = false`
- `real_order_submission = false`
- global maximum drawdown kill threshold: 15%

No wallet, key or authenticated order-submission path belongs in the canonical runtime.

## Canonical architecture

```text
Public Polymarket market data
        |
        v
V7 strategy/model sleeves
        |
        v
Canonical execution / intent routing
        |
        v
Single V7 PAPER runtime process group
        |
        +--> single append-only execution ledger / writer
        +--> learned execution and joint-state evidence
        +--> account-level capital allocator
        +--> global portfolio guard / kill switch
        +--> Prometheus / Grafana V7 monitoring
        +--> exact-SHA PAPER validation and deploy
```

Canonical runtime entrypoint:

```text
scripts/paper_v7_execution_loop.sh
```

Canonical PAPER config:

```text
config/paper_v7.json
```

Canonical run root:

```text
runs/paper_v7_live
```

Canonical execution ledger:

```text
runs/paper_v7_live/ledger/execution.jsonl
```

Canonical monitoring:

```text
monitoring/exporter_v7.py
monitoring/prometheus_v7.yml
monitoring/grafana/dashboards/polymarket-v7.json
```

Canonical server updater:

```text
ops/update_server_v7.sh
```

## Runtime sleeves

The runtime may host multiple economic sleeves, but none owns a private broker, private capital account, private ledger or separate deployment system.

### Professional market maker

Primary market-making sleeve. It uses action-specific `JOIN` / `IMPROVE` / `FADE` / one-sided / withdraw decisions, inventory-aware pricing, causal public-flow replay, queue-aware fillability, executable full-depth marks and canonical-ledger evidence. Trading PnL, maker rebates and liquidity rewards are separate quantities. Bounded PAPER exploration receives no promotion credit.

Key files:

```text
config/v7_professional_market_maker.json
scripts/v7_market_maker_core.py
scripts/v7_market_maker_model.py
scripts/v7_market_maker_rewards.py
scripts/v7_market_maker_worker.py
scripts/v7_market_maker_status.py
```

### Fast structural arbitrage

C++ WebSocket L2 research/execution substrate with dual exchange/receive clocks, lineage invalidation, executable depth and strict freshness semantics.

```text
src/fast_arb.cpp
src/fast_ws.cpp
src/fast_runtime/
config/fast_arb_v7_shadow.json
```

### Graph / relative value

Native V7 multi-leg relative-value execution. Queue affects fillability only; it never creates capital capacity. Promotion economics use direct empirical joint states and explicit partial/unwind losses.

```text
scripts/v7_graph_rv.py
scripts/v7_graph_rv_executable_intents.py
scripts/v7_graph_cost_vector.py
```

### Hard Arb

Native V7 structural complete-set execution research with authoritative fees, full depth, sequential leg revalidation and partial unwind.

```text
scripts/v7_hard_arb_guard.py
```

### Micro Taker

Selective short-horizon executable round-trip sleeve with entry/exit depth, fee, slippage and adverse-markout accounting.

```text
scripts/v7_micro_taker_worker.py
scripts/v7_micro_taker_core.py
```

### Ranking / PCA / Local Factor

Research sleeves remain V7-native and horizon-separated. They do not own runtime state or deployment.

## Canonical execution evidence

Quoted edge is not PnL. Promotion-grade observations are bound to exact model SHA and canonical identities for opportunities, bundles, orders, legs, fills, positions and events.

The economic decision rule accounts for:

```text
completion probability × fill-conditioned post-cost alpha/spread capture
- partial/unwind loss
- fees
- slippage
- adverse markout
- capital cost
- latency cost
```

For multi-leg execution, direct empirical joint completion/state evidence is canonical. Products or minima of marginal fill probabilities are not substitutes.

The ledger records append-only execution events and matured executable markouts. Economic assessment stresses the same frozen observations at 1x, 1.5x and 2x costs rather than reselecting trades.

Relevant modules:

```text
scripts/v7_execution_ledger.py
scripts/v7_ledger_spool.py
scripts/v7_execution_evidence.py
scripts/v7_canonical_economics.py
scripts/v7_joint_execution_policy.py
scripts/v7_learned_execution_hardened.py
```

## Capital and risk

There is one account-level allocator and one global portfolio guard:

```text
scripts/v7_capital_allocator.py
scripts/v7_portfolio_guard.py
```

Sleeve allocations are capacity budgets, not independent copies of the account. Cash, gross exposure, market/event exposure, inventory, drawdown and kill state must reconcile at account level.

## Public trade recorder

V7 has a standalone public recorder; it is not embedded in a legacy engine or broker:

```text
build/polymarket_v7_trade_recorder
src/v7_trade_recorder.cpp
```

It writes the causal public tape used by the PAPER execution models under the canonical run root.

## Build and tests

Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake pkg-config libcurl4-openssl-dev libboost-all-dev python3
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
ctest --test-dir build --output-on-failure
```

Primary C++ executables:

```text
polymarket_v7_trade_recorder
polymarket_fast_arb_shadow
```

The active trading runtime itself is `scripts/paper_v7_execution_loop.sh`; there is no generic `polymarket_engine` fallback.

## Run V7 PAPER

After building the recorder/Fast components:

```bash
bash scripts/paper_v7_execution_loop.sh
```

The loop refuses unsafe V7 configuration and requires PAPER/authenticated-disabled invariants.

## Monitoring

Only the V7 monitoring plane is authoritative. Grafana/Prometheus read canonical V7 runtime and ledger state. No “highest version wins” or version-agnostic legacy dashboard selection is used.

## Repository rule

Executable/control-plane code must not reintroduce V3-V6, `paper.example`, `polymarket_engine`, legacy broker state, alternate PAPER loops, duplicate maker engines, duplicate ledgers, duplicate state writers, or generic deployment entrypoints. `tests/test_no_legacy_runtime.py` enforces this fail-closed rule.

## Further documentation

- `docs/EXECUTION_EVIDENCE_V7.md`
- `docs/PROMOTION_EVIDENCE_BINDING.md`
- `docs/EXTERNAL_INTELLIGENCE.md`

Anything not consistent with this V7-only architecture is non-authoritative and should be removed rather than preserved as executable compatibility code.
