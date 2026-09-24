# Native graph ownership and bounded compilation checkpoint

Base: `research/unified-exact-arb-graph`,
`f50147e18e02ebed76cabe900b708e4d9479e603` plus the uncommitted changes described
below. This is local engineering evidence, not a deployed exact-SHA artifact,
an operational native observer, or an economic research verdict.

## Implemented

- `include/pm/v7_exact_arb_graph_runtime.hpp`: bounded native frame evaluator,
  with three owned generation slots, atomic ownership transfer and an SPSC
  retirement queue. Only the control thread destroys/replaces backing storage.
  Pending successes are cancelled by source invalidation, including concurrent
  activation races. Source and relation deadlines are enforced on the causal
  monotonic clock. No order, OMS, ledger or execution interface exists.
- Decisions occur after all changed books in a decoded frame have been supplied;
  each affected relation is evaluated once in deterministic handle order. No
  intermediate mixed-leg frame state is emitted. Different claims at reused
  book handles clear cached books; connection epoch changes clear every book.
  Future timestamps, regressing versions and clock inversions fail closed.
- Every observation carries both the full graph generation and the bounded
  bundle digest, plus frame/epoch, proof handle and all leg versions. A full
  graph alone cannot identify handles across different hotsets.
- `scripts/v7_exact_arb_native_compile.py`: shared static/runtime lowering,
  rational payoff re-verification, node/leg identity checks, and bounded native
  projection (128 selected tokens, 512 directional relations, 64 dependencies
  per token). A graph with over 65,536 nodes can be projected without enlarging
  native bounds. Unsupported relations are explicitly excluded.
- `scripts/v7_exact_arb_hotset_selection.py`: publishes the proof-carrying
  native payload/digest in the same atomic file as subscription membership and
  source expiration. REST evidence remains non-actionable. The payload does
  not independently attest actual settlement semantics.
- Native tests exercise 20,000 concurrent publications, source invalidation,
  expiry, reconnect/claim remapping, future data, malformed indices, affected-only
  evaluation and no C++ heap allocation **or deallocation** on the feed thread.
- The existing exact-arb review workflow now includes these tests and a separate
  ThreadSanitizer matrix entry. This edit is not evidence of a completed Linux
  CI run.

## Validation

| Check | Local observed result |
|---|---|
| `/usr/bin/python3 -m pytest -q` | 2,365 passed, 1 existing skip, 384 existing numerical warnings |
| Configured Release CTest, `-j 4` | 396/396 passed; new target rebuilt, unrelated targets used the existing build tree |
| Native runtime target, Release | PASS; concurrency also passed 20 repeated executions before final assertion additions |
| Native runtime target, Debug | PASS |
| Native runtime target, combined ASan/UBSan | PASS |
| Standalone native runtime, Clang ThreadSanitizer | PASS, including allocation/deallocation assertions |
| Frozen lane/economics/multi-engine diff | Empty |
| `git diff --check` | PASS |

The first broad Python invocation used Homebrew's Python 3.14 pytest launcher
and failed collection because that environment lacked NumPy and the repository
root import path. The rerun used the existing system Python 3.9.6 with pytest
8.3.5 and NumPy 2.0.2. No tests were removed or relaxed to obtain the results.

## Explicit remaining integration work

1. Implement the off-path native loader: verify payload digest, PAPER/model/source
   identity and rational proofs; bind selected token IDs to decoder handles;
   bind venue ticks, minimum order/precision, start/end windows and finite paper
   resource budgets. Structural admission in `OwnedGeneration` is not a semantic
   proof verifier and must never be exposed as one.
2. Connect the kernel to the dedicated causal WS observer, with bounded telemetry
   and complete candidate book evidence. Candidate episode IDs, lifetime handling,
   durable deduplication and resource reservations are still required. Kernel
   observations are **not** independently executable opportunities or PnL.
3. Validate parity and replay at the real frame boundary, then replace Python
   per-update graph traversal. The current launcher still uses the Python graph
   shadow; the new kernel is not yet its operational replacement.
4. Complete the broader semantic/execution/economic gaps listed in the preceding
   engineering checkpoint, exact-SHA Linux gates and official deployment.

## London and economics

A fresh read-only `ssh polymarket` attempt failed authentication for the configured
`enrico@100.104.183.109` target. This does not establish that London is down.
No deployment, merge, real order or champion authority change occurred.
Live causal exposure, coverage, opportunity rate, fillability and economic value
remain unmeasured for this implementation. `research_decision = null`.
The overall goal remains active; this checkpoint does not meet completion criteria.
