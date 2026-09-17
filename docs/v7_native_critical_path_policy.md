# V7 Native Critical-Path Policy

The canonical crypto critical path is native C++.
This is a latency and correctness policy, not a language preference.

Critical path:

`market-data receive -> decode -> causal state -> features -> signal -> candidate -> portfolio arbitration -> risk/capital admission -> OMS intent -> execution adapter`

Every stage above must remain native and event driven. Python may orchestrate,
research, monitor, report and recover. It must not be required for a trading
reaction.

The hot path must not depend on filesystem polling, synchronous REST market-data
fetches, databases, process spawning, cross-process JSON IPC, synchronous
telemetry, unbounded allocation or fixed sleep polling. Exchange JSON may be
parsed only by the bounded native decoder at the ingress boundary.
Cold-start and recovery may use REST, config files and Python tooling because
they are outside the causal reaction path. Evidence persistence is asynchronous.
A queue overflow, gap, stale state, wrong lineage or wrong identity fails closed.

C, Rust, io_uring, AF_XDP, DPDK, kernel bypass or a vendor-native API may replace
a C++/kernel component only after a controlled same-semantics benchmark shows a
material p99 improvement, no p99.9 regression, zero silent drops and no change
to risk or authority semantics.

Targets are measured with a monotonic clock. The initial trigger-to-admission
p99 target is 300 microseconds; the stretch target is 100 microseconds. These
are engineering targets, not claims of achieved performance.

No component introduced for latency may create another allocator, risk owner,
OMS, inventory owner, execution owner or ledger authority. Promotion order is:
unit/parity -> deterministic replay -> live zero-authority shadow -> prospective
London PAPER -> single-owner cutover -> separate real-money decision.

Gamma API and all remote metadata discovery are cold-path only. The HFT engine may consume pre-resolved metadata but may not wait on Gamma.
