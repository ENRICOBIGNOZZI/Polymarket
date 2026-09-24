# Fee accounting: measured differences, not assumed parity

Branch `research/unified-exact-arb-graph`; base HEAD
`f50147e18e02ebed76cabe900b708e4d9479e603` plus local uncommitted changes.
PAPER / SHADOW only. No orders, authentication, capital, promotion, merge or deployment.

## All 122 quantity differences attributed

The native sizing test now compares four paths using the same 3,000 seeded
nonzero-fee binary fixtures and unchanged reserve/order lattice:

1. Actual frozen champion, using its own floating arithmetic and shared-slice sweep.
2. Arbitrary-precision rational shared-slice sweep, including the champion's stopping epsilon.
3. Exhaustive order-lattice optimization under that same shared-slice fee model.
4. The graph's exact per-leg-L2 fee model and bounded optimizer.

Of the original 122 differing quantities:

| Difference under this counterfactual decomposition | Cases |
| --- | ---: |
| Greedy search vs exhaustive optimization only | 99 |
| Fee fragmentation only | 3 |
| Both search and fragmentation | 20 |
| Floating arithmetic changed the selected quantity | 0 |

Across all 3,000 trials, search affects 163 intermediate comparisons and
fragmentation affects 67. They sometimes cancel; these counts must not be added
to obtain the 122 end-to-end differences. At the champion's quantity, all **827**
reported-money differences arise from shared-slice vs per-leg-level fee
fragmentation in this sample; none requires floating arithmetic or display
round-versus-floor as the explanation.

This is an accounting counterfactual decomposition, not causal attribution of
live profits. Fixtures include sub-venue-minimum quantities and are not a market
sample. Exact quantity parity remains false: the frozen greedy rule and a global
optimizer with a different fee partition are different objectives. No champion
code or assertions were weakened to make the comparison green.

Reproduction:

```sh
cmake --build build-Release --target pm_v7_exact_arb_order_sizing_tests
build-Release/pm_v7_exact_arb_order_sizing_tests --attribution-report /new/nonexistent/output.json
```

The artifact refuses to overwrite an existing file. Full differing inputs and
intermediate quantities/fees are in
`exact-arb-binary-sizing-attribution-v2-20260924.json` (the earlier quantity-only
artifact is retained separately). SHA256 identities for this run:

- test source: `6fc3a330a1766e3b92fc9324f1a9b809e6bcdb165922f25f64a44137094677a8`
- Release executable: `28686aae344637b3d90a9e909536d1b03ca2f9da1cde11cafa9165b42ebca96b`
- v2 artifact: `399abb0616c78544617415ed37b0bba238c2e74d0f06c98808fe70869952a634`

## Public settlement observation changes the next action

`v7_exact_arb_settlement_fee_audit.py` now collects and replays a bounded,
read-only Polygon RPC transcript. It pins a finalized block hash, checks chain
137 and canonical block identity, joins OrderFilled to OrdersMatched, rejects
incomplete groups and reconstructs maker contributions for complementary,
split and merge matches. Maker amounts must reconcile before any per-fill or
per-price-level comparison is made. No REST book enters an execution decision.

Observed block `0x59fe775`, hash
`0x1c818100ff29e63abbbc19ad2b68c7d6bf1499cae7ff8ae03801bc5f9218974c`:

- 94 exchange logs; 55 OrderFilled events; 20 taker matches.
- 19 positive fees, one zero fee. A zero observation does not establish a zero rate.
- All 19 positive fees are compatible with FLOOR at aggregate match VWAP under
  one of the explicit rate hypotheses (8 at .05, 10 at .07, one at .03).
- For example, log 286 records 12 shares at .999 and a fee of .00059. At an
  assumed .05 rate, floor predicts .00059; the current half-up model predicts .00060.
- Multi-maker log 494 matches only aggregate-VWAP floor among the six tested
  aggregation/rounding models at an assumed .05 rate.

These are **fitted hypotheses**, not independently established historical rates.
One block, one RPC provider, and observed logs do not verify completeness,
deployed source binding, future fee policy or matching behavior. All verification
flags stay false. The raw transcript and recomputable projection are archived at
`evidence/exact-arb-settlement-fees-20260924/18606d39458d70061339d210d72f2a78a2bae29a10335ad4243d7b2bcd6e1e31.json`.

The [official fee documentation](https://docs.polymarket.com/trading/fees)
specifies the curve and five-decimal precision but does not establish our
per-L2 half-up convention. The pinned
[Trading source](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/mixins/Trading.sol)
accepts explicit fee amounts from the matcher; the
[fee contract](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/mixins/Fees.sol)
caps them rather than implementing the off-chain fee curve. Inspecting this
source alone cannot attest the matching service's rounding convention.

Next fee action: bind independently observed schedules to additional settlement
samples and distinguish aggregate versus per-fill rounding before changing the
research accounting model. Do not tune the optimizer to mimic an unverified
fee convention, lower reserve, or label model optimum as venue optimum.

## Other changes and remaining work

Native raw-frame replay now preserves the recorded `receive_wall_ms`. Python
validates it when present, but older tapes remain explicitly without this field.
Monotonic execution is unchanged even when wall time jumps. This is groundwork
for honest hourly attribution, **not completed hourly orchestration or verified UTC**.

Capital release/capacity, maker calibration, broader verified relations,
worst-case load, side-by-side champion latency, hourly multi-session reporting,
official clean Linux gates and London deployment remain open. Fresh read-only
SSH with `RemoteCommand=none` still fails authentication; no current deployed SHA
or London health was inferred. `research_decision = null`.

## Validation

- Full local Python suite: **2,723 passed**, one existing skip, 384 existing warnings.
- Full configured Release CTest: **415/415 passed**.
- After those full runs, seven additional fee-audit cases were added for
  BUY/SELL complementary/split/merge reconstruction and wrong-chain/reorg/future
  blocks. The final fee-audit module passes **26/26** tests.
- Four scoped native/integration tests pass in Release, Debug, combined
  ASan/UBSan and TSan, including the strengthened sizing oracle and real C++ raw decoder.
- The first TSan sizing run hit the unchanged 120-second test limit. Replacing
  repeated rational normalization in the independent oracle with a common
  denominator and arbitrary-precision integer arithmetic reduced the final
  TSan sizing run to 32.22 seconds (Release .39s, Debug 5.66s, ASan/UBSan 14.90s
  under concurrent local runs). No fixtures, assertions or timeout were relaxed.
  The optimized oracle reproduced the v2 report **byte-for-byte** (`cmp` passed).
  Final test-source SHA256: `6fd5ffd1c2d17c6ebe3883c5a49d7c7c845345d53520cf770918966a366d695e`;
  final Release executable SHA256: `442ee2716a113f75347a4a0731fe186e6f063b39c75288f995dcdab381094609`.
- Frozen champion lane, economics and multi-engine files have no diff;
  `git diff --check` passes. These are local gates, not clean official Linux CI.
