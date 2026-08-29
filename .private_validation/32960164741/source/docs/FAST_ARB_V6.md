# Fast Arbitrage V6 — Event-Driven Shadow Plane

## Scope

V6 adds a low-latency, read-only/shadow arbitrage plane without replacing the V4 paper champion. The purpose is to measure whether short-lived opportunities survive real public-feed latency, full displayed depth, fee schedules, slippage, non-atomic multi-leg execution risk, and conservative fixed costs.

The binary deliberately contains no authenticated order endpoint, wallet code, private-key lookup, `--execute` flag, or live PnL booking. A fast opportunity is evidence, not a fill.

## Engine planes

```text
                           ONE MODEL-GOVERNANCE CONTROL PLANE
                                         |
                  explicit live champion | approved research integration
                                         v
+----------------------+       +-----------------------+       +----------------------+
| Fast data/arb plane  |       | Slow alpha plane      |       | Portfolio/execution  |
|                      |       |                       |       |                      |
| CLOB market WS       |       | B1 pair stat-arb      |       | V4 paper broker      |
| REST snapshot resync |       | B2 PCA/factor baskets |       | queue/partial fills  |
| binary/NegRisk/logic |       | terminal/fair value   |       | unwind/drawdown kill |
| external latency RV  |       | rewards diagnostics   |       | durable ledger       |
| maker shadow         |       |                       |       |                      |
+----------+-----------+       +-----------+-----------+       +----------+-----------+
           |                               |                              |
           +-------------------------------+------------------------------+
                                           |
                                  shared evidence contracts
                                           |
                                hourly theory/research plane
```

The planes are separate because their economic objects differ:

- hard arbitrage is a statewise payoff inequality;
- external-feed latency and statistical arbitrage are expected-value hypotheses;
- maker quoting is a fill/adverse-selection problem;
- execution and portfolio risk are not probability estimators;
- research automation may generate candidates but cannot select the live champion.

## Fast market-data plane

`polymarket_fast_arb_shadow` uses:

1. REST market discovery and a full initial `/books` snapshot;
2. the public CLOB market WebSocket for `book`, `price_change`, `tick_size_change`, and `last_trade_price` events;
3. in-memory books updated at level granularity;
4. token sharding across WebSocket workers;
5. an application `PING` every ten seconds;
6. reconnect with bounded backoff;
7. periodic REST snapshot replacement to repair missed deltas;
8. process recycling to refresh the market universe.

A token update recomputes only:

- its binary complete set;
- its explicit external signal;
- its maker shadow quote;
- its verified NegRisk event group;
- logical relations incident to that market.

No CSV or Python process lies on the WebSocket-to-decision path. Persistence and theory analysis are downstream.

## Structural arbitrage library

The pure C++ library is in `include/pm/fast_arb.hpp` and `src/fast_arb.cpp`. It is independent of sockets and filesystem state and is unit tested.

### Binary complete set

For a binary market,

```text
YES + NO = 1.
```

The scanner buys both asks only when the statewise payoff floor exceeds depth-walked cost, taker fees, configured slippage, and a latency penalty.

### Complete NegRisk basket

For a verified, non-augmented, complete NegRisk event,

```text
sum_k YES_k = 1.
```

The runtime checks the event object, rejects augmented groups, requires every listed member to remain tradable, obtains every member book, and evaluates the full basket at common executable share size.

### NegRisk conversion

For source outcome `i`, the structural conversion is

```text
NO_i -> {YES_j : j != i}.
```

The scanner walks the source NO ask, every target YES bid, taker fees, slippage, a fixed conversion-cost floor, and latency. It is logged as `ONCHAIN_CONVERSION_NON_ATOMIC`; the shadow process does not call a contract or submit an order.

### Logical relations

Verified relations live in `config/fast_arb_relations.csv`; the engine never invents a hard logical edge from text similarity.

Supported relation types are:

- `IMPLICATION`: if `A subset B`, then `NO_A + YES_B >= 1`;
- `MUTUAL_EXCLUSION`: if `A` and `B` cannot both occur, then `NO_A + NO_B >= 1`;
- `EXHAUSTIVE_PAIR`: if at least one of `A` and `B` must occur, then `YES_A + YES_B >= 1`;
- `EQUIVALENCE`: expanded into both implications.

An empty relation manifest is valid and safer than an unverified graph.

## Non-hard sleeves

### External-feed latency relative value

`data/external_signals.csv` retains the common interface

```text
market_key,q_yes,confidence,source,timestamp
```

The runtime evaluates both YES and NO, selects the larger costed edge, rejects stale signals, penalizes low confidence, and labels the result `MODEL_RISK_EXTERNAL_SIGNAL`. This is not reported as hard arbitrage.

### Maker complete-set shadow

The engine computes post-only prices for both binary outcomes and subtracts an explicit one-sided-fill penalty. It is labeled `NON_ATOMIC_PASSIVE_TWO_SIDED`. It neither places quotes nor treats displayed depth as a fill.

## Cost and depth rules

Every taker structure uses the entire displayed ladder needed for a common share size. Candidate size must satisfy:

```text
shares >= max(min_order_size across legs, configured minimum shares)
capital <= configured maximum notional
net edge >= configured minimum edge
```

The cost stack is:

```text
displayed depth walk
+ configured slippage
+ market fee function
+ latency penalty
+ fixed conversion cost where applicable.
```

The engine records both raw displayed edge and net edge. It never subtracts half-spread a second time after using executable asks or bids.

## Runtime evidence

The fast run directory contains:

```text
fast_arb_opportunities.csv   state-change evidence
fast_arb_latest.csv          atomic current opportunity snapshot
fast_arb_latency.csv         sampled feed/decision latency
fast_arb_status.json         atomic health and quantiles
fast_arb_errors.csv          reconnect/data errors
fast_runtime.log             process stdout/stderr
```

`fast_arb_status.json` explicitly states:

```json
{"mode":"shadow","real_order_submission":false}
```

The champion selector writes `runtime_planes.csv`, showing champion and fast-shadow liveness independently.

## Continuous runtime

The explicit champion selector supervises two sibling processes:

```bash
bash scripts/paper_latest_loop.sh
```

- the selected V4/VN champion remains the only paper portfolio/execution process;
- the fast process remains a shadow evidence process;
- failure of the champion exits the selector so the service manager can restart it;
- failure or scheduled recycling of the fast process restarts only the fast plane;
- `POLYMARKET_FAST_ARB_REQUIRED=1` prevents silent deployment without the binary.

Useful overrides include:

```text
POLYMARKET_FAST_ARB_ENABLED
POLYMARKET_FAST_ARB_REQUIRED
POLYMARKET_FAST_ARB_MARKETS
POLYMARKET_FAST_ARB_MIN_LIQUIDITY
POLYMARKET_FAST_ARB_SHARD_SIZE
POLYMARKET_FAST_ARB_SNAPSHOT_SECONDS
POLYMARKET_FAST_ARB_RECYCLE_SECONDS
```

## Hourly operational scheduler

`.github/workflows/fast-arb-hourly.yml` runs at minute 7 of every hour. It:

1. builds Release;
2. runs the complete test suite;
3. runs a bounded public-data shadow probe;
4. produces latency and opportunity evidence;
5. applies a ten-basis-point additional cost stress;
6. uploads a thirty-day evidence artifact.

The workflow receives no credentials and asserts that both runtime and candidate reports remain shadow-only.

## Hourly theoretical-research scheduler

`.github/workflows/arb-theory-hourly.yml` runs at minute 37. It downloads recent operational evidence and calls `scripts/arb_theory_scheduler.py`.

The scheduler:

- re-states the payoff identities being tested;
- separates hard and model-risk sleeves;
- estimates feed and decision latency quantiles;
- reconstructs completed opportunity lifetimes from state transitions;
- applies additional cost stress;
- computes a run-level bootstrap lower bound;
- derives a candidate threshold that can only become more conservative automatically;
- generates candidate JSON, Markdown, and a compilable C++ policy header;
- updates one persistent research issue;
- creates or refreshes a draft `research/auto-fast-arb-policy` PR only after the research gate passes.

The automated branch is reset from current `main`, changes no champion manifest, remains draft, and cannot merge itself. Even `promotion_ready=true` only permits an integration review under the existing governance workflow.

## Research and promotion gates

The research gate requires multiple independent runs, enough WebSocket messages, enough executable observations, cost-stress survival, and bounded decision latency.

The stricter promotion-evidence gate requires, among other conditions:

- at least 24 runs;
- at least 100,000 WebSocket messages;
- at least 50 hard executable observations;
- at least 80% hard-opportunity survival under an extra ten-basis-point cost shock;
- positive bootstrap lower bound for run-level shadow opportunity profit;
- decision latency p99 at most five milliseconds;
- enough completed opportunity episodes;
- opportunity lifetime p10 larger than end-to-end latency p99 plus a safety margin.

These are evidence gates, not claims of realizable live PnL. Public timestamps, network routing, non-atomic orders, missing user-channel acknowledgements, and on-chain conversion latency remain material.

## What is intentionally not accelerated

B1/B2 refitting, terminal probability estimation, calibration, and OOS selection stay on slower clocks. Recomputing a two-week PCA model on every book delta would increase noise and resource use without creating execution speed. Their latest frozen signals can later be consumed by the fast execution plane through a stable in-memory or atomic-file interface, but estimation and execution remain distinct.

## Non-negotiable safety boundary

V6 does not make the tiny-live pilot automatic. Real execution still requires a separately reviewed authenticated adapter, user-channel reconciliation, balance and allowance checks, FOK/FAK semantics, unmatched-leg controls, production kill switches, and explicit approved integration. No scheduler may infer authorization from positive paper evidence alone.
