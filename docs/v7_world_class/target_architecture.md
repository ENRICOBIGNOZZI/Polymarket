# V7 target architecture

```text
official/external sources -> immutable ingress -> point-in-time validation
-> strategy snapshots -> global risk -> execution policy -> V7 OMS
-> isolated signer -> CLOB V2 -> private-state reconciliation
-> append-only double-entry ledger -> independent verifier -> attestation
```

The parallel research path is immutable ingress -> deterministic replay -> PAPER
simulator -> model governance. Research has zero execution authority. There is
one V7 runtime, OMS, ledger, capital allocator, risk authority, private-state
authority, model registry, and monitoring plane.

[`config/v7_execution_modes.json`](../../config/v7_execution_modes.json) is
the capability matrix. [`include/pm/v7_execution_mode.hpp`](../../include/pm/v7_execution_mode.hpp)
and [`src/v7_execution_mode.cpp`](../../src/v7_execution_mode.cpp) make the
mode typed in the C++ runtime; legacy booleans are compatibility checks, not an
authority selector.
