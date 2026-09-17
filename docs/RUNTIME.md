# V7 Runtime

`scripts/paper_v7_execution_loop.sh` runs the single canonical `CRYPTO_SETTLEMENT_ENGINE` continuously under one allocator, risk owner, OMS, inventory owner and ledger writer. The runtime is PAPER-only: authenticated execution, real order submission, real capital risk and new-risk authority remain disabled.

The crypto engine can compare internal maker, informed-taker, cancellation and no-action components, but those components do not become independent economic engines or independent capital authorities.
