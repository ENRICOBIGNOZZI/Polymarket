# Native pressure and exact prefix sizing — performance gate remains open

Branch `research/unified-exact-arb-graph`; base HEAD
`f50147e18e02ebed76cabe900b708e4d9479e603` plus local uncommitted changes.
PAPER / SHADOW only. No orders, authority, merge, promotion or deployment.

## What the earlier benchmark did not establish

The prior microbenchmark used four levels and evaluated relations individually.
That does not establish runtime behavior at the configured bounds: 128 books,
512 relations, 16 legs, 64 dependencies per token and 1,024 levels per side.

`tests/bench_v7_exact_arb_pressure.cpp` now exercises the actual
`NativeGraphRuntime`, not a stand-in arithmetic loop. Thirty positive synthetic
profiles cover 2/3/4/8/16 legs, 4/64/1,024 levels and either one or 128 changed
tokens; another profile covers the deepest raw-nonpositive case. The structural
portfolios are distinct, have balanced fanout and pass native structural
admission. They are **not independently attested market payoff relations**.

Each frame checks that exactly the affected relations emit once in stable order.
A deliberately undrained 64-entry SPSC diagnostic queue records enqueued versus
dropped observations; it never waits on a writer. Timings separate frame/book
update plus dependency collection, evaluation plus bounded sink, and total frame
compute. They do not include the complete WS decoder, full-book evidence copies,
serialization, network or the 30-worker London runtime.

The same executable measures the actual frozen `pure_arb::sweep` before, during
and after a second thread runs graph frames. Bracketed atomic progress counters
show graph evaluations completed during champion sampling, not merely that a
thread existed. This is one local host without CPU affinity or randomized trials.

## Measured bottleneck and bounded correction

The baseline repeatedly walked filled levels and recalculated their fees for
each candidate quantity. A dense frame took about 1.92 seconds locally.

The runtime now owns an approximately 769 KiB preallocated sizing workspace.
For each relation, reachable depth receives exact cumulative quantity, cash and
rounded per-L2 fees. Candidate queries binary-search those prefixes and calculate
at most the remaining partial-level fee for each leg. The workspace is rebuilt
per invocation, never reused as a stale book/fee cache. No native feed allocation
or deallocation was added; existing allocation assertions still pass.

Only depth reachable under the quantity/resource bound is prepared. Raw-negative
smallest-order rejection retains its fast path. Prefix failure falls back to the
original walker, preserving when numeric overflow is reported and how the search
budget/certificate is counted. Reserve, fee formula, rounding convention,
inventory constraints, search order and the 512-quantity runtime budget are unchanged.

Local Darwin arm64, before versus final prefix implementation:

| 16-leg fixture | Before frame ms | After frame ms | Before/after samples |
| --- | ---: | ---: | ---: |
| 4 levels, one token / 64 affected relations | 1.952 | 1.042 | 52 / 96 |
| 64 levels, one token / 64 affected relations | 212.742 | 14.505 | 1 / 7 |
| 64 levels, full frame / 512 relations | 1704.897 | 117.309 | 1 / 1 |
| 1,024 levels, one token / 64 affected relations | 239.212 | 26.447 | 1 / 4 |
| 1,024 levels, full frame / 512 relations | 1916.402 | 212.741 | 1 / 1 |
| 1,024 levels, raw-nonpositive full frame | 11.671 | 11.571 | 9 / 9 |

These are descriptive p50 values, often just one expensive sample. They are not
robust tail estimates or a formal worst-case CPU bound. Duration limits are soft
and checked after each bounded frame. The maximum-depth positive case improves
about **9x**, but **213 ms remains too slow to call this worst-case gate closed**.
The number of quantities examined per relation is unchanged across all profiles;
the dense cases still exhaust 512 evaluations and do not gain optimality proof.

## Champion pressure observation is not a non-regression certificate

The final run collected one million four-level champion sweep samples per arm.
During its sampling interval, 292 graph relation evaluations completed; the large
graph frame itself had not yet completed. Champion p99 stayed at 125 ns, while
p99.9 moved from 125 ns before load to 166 ns during load and back to 125 ns after.
That increase is recorded, not hidden by the stable p99. Timer quantization,
scheduling, affinity, thermal effects and independent repeated episodes were not
controlled. This does not establish causality or acceptable London performance.

No London latency gate is marked green. Full decode/capture/serialization and
champion decision-lane/queue/drop measurements under protected CPU topology remain
required. Increasing fanout, reducing depth or lowering a sizing/fee/reserve
standard to manufacture a faster successful result was not used.

## Reproducible evidence

- `exact-arb-pressure-baseline-20260924.json`: pre-prefix baseline.
- `exact-arb-pressure-prefix-20260924.json`: first prefix measurement, retained.
- `exact-arb-pressure-prefix-final-20260924.json`: final overflow-preserving version.
  Receipt SHA256: `0ce50e7ec2b034ff5bbb6d152022c6998b885b309fd339609955981f18e6a211`.

Receipts contain source/binary hashes, host architecture, parameters, all 31
profiles, drops and before/during/after champion measurements. The runner validates
matrix completeness, fanout, counters, work limits, quantiles, parameters and
unchanged inputs, then atomically publishes without overwriting earlier evidence.
Hashes identify artifacts; they do not fabricate official build/deployment binding.

```sh
cmake --build build-Release --target pm_v7_exact_arb_pressure_bench
python3 scripts/v7_exact_arb_pressure_bench.py \
  --binary build-Release/pm_v7_exact_arb_pressure_bench \
  --output /new/nonexistent/pressure-receipt.json \
  --case-ms 100 --pressure-ms 1000 --pressure-depth 1024
```

## Correctness and integration

The sizing tests compare all result and certificate fields between cached and
uncached paths over the existing 9,000 seeded cases. Added deep BUY/SELL ladders,
partial tails, workspace reuse, 0/1/2/16/512 budgets and extreme fee overflow
also agree. The independent rational oracle remains unchanged. The original
fee-attribution artifact is reproduced **byte-for-byte** (`cmp` passed): 122
quantity and 827 monetary-model differences versus champion still remain.
This optimization does not resolve the distinct venue fee-model question.

Stress testing also exposed that a legitimately slow computation can finish after
its own evaluation deadline. The execution bridge now distinguishes inconsistent
deadlines from a valid but expired decision, after validating candidate inputs.
The study runner records the latter as `native_decision_expired`, reserves no
capital and schedules no simulated order. Corrupt books/deadlines still fail
validation; they are not concealed as ordinary latency censoring.

A full Release rebuild, rather than just rerunning cached CTest executables,
found a missing graph-control link dependency in the causal observer test.
That dependency is fixed, and the real causal-observer target is now built and
run by the scoped review workflow alongside the pressure contract test.

Final local validation: full Release rebuild passed; Python finished with
**2,788 passed, 1 skipped** (384 warnings); full Release CTest finished with
**418/418 passed**. Eight scoped native/integration tests also passed in each of
Debug, ASan+UBSan and TSan. Those scoped runs preceded the final Python expiry
validation adjustment, which the concluding full Python and Release runs cover.
`git diff --check` passed. These are not official clean Linux release gates.
No test assertion or timeout was relaxed. Frozen champion lane, economics and
multi-engine files have no diff.

## Next gate, without endless optimization

Do not call the configured worst-case performant or the strategy profitable.
Measure the actual verified hotset's degree/depth distribution and queue pressure,
then enforce resource isolation and assess any remaining bounded computation
work against causal opportunity lifetimes. Independent semantics, maker/risk
calibration, capital capacity, live hourly orchestration, official release and
London causal PAPER evidence remain necessary. `research_decision = null`.
