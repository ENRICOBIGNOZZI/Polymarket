# V7 Multi-Crypto — Final Repository Status

This document describes repository readiness only. It is not a deployment or an
economic-profit certification.

## Completed in the integrated candidate

- six-asset BTC/ETH/SOL/XRP/DOGE/BNB M5/M15 discovery and verified settlement mappings;
- persistent public venue feeds, PM BookHub and six-asset OracleHub;
- one zero-authority SHADOW supervisor plus causal ContractState binding;
- causal feature/label tape, idempotent normalized shocks, derivative and cross-crypto inputs;
- training-only shock calibration, repricing research and fail-closed readiness gates;
- pooled residual benchmark with chronological splits and nested ablations;
- one global coordinator, one shared reservation projection and one canonical ledger writer;
- direct bounded Unix IPC for forward candidates and durable ledger ACK after canonical append;
- native in-memory L10 -> FAK PAPER taker execution with no REST reread;
- Python Decimal <-> C++ fill/cost/fee parity on a common fixture;
- global PAPER cash checkpoint/reconciliation and lead-lag risk inclusion;
- prospective protocol freeze, forward accounting and cross-asset risk/correlation reports;
- London physical-AZ identity, provisioning, bootstrap, benchmark and shootout contracts;
- lossless/restart-safe retention and immutable provenance repairs from the independent audit.

All new authority surfaces remain PAPER/SHADOW only. Automatic promotion and automatic
regional cutover are false by construction.

## What is deliberately not declared complete

- **Economic edge:** current repository evidence is insufficient to promote thresholds or
  claim multi-crypto profitability. Missing/pending outcomes remain missing/pending.
- **Maker fill-conditioned model:** the verification fixture reports one independent fill
  cluster versus a minimum of three; state remains `INSUFFICIENT_FILL_CONDITIONED_EVIDENCE`.
- **End-to-end latency:** internal compute gates pass, but venue/network/matching-engine
  percentiles are not proven by local synthetic timing.
- **London deployment:** no AWS credentials or launch network/admin parameters are available
  on this Mac. No EC2 host has been created and no run/ledger generation has been migrated.
- **Real money:** authenticated execution, real order submission and real capital remain off.

## Activation rule

ETH/SOL PAPER (and later XRP/DOGE/BNB) may only be activated after a protocol is frozen
from pre-forward information, the required independent evidence is present, exact-SHA
verification passes, account state is reconciled, and the global coordinator grants the
explicit PAPER-forward authorization. Passing code tests alone is insufficient.

London may only become the PAPER host after all three physical AZs are benchmarked with the
same SHA/hardware/load, the formal 24-hour evidence passes, one AZ is selected from measured
end-to-end evidence, and the old writer is drained/sealed before the new ledger generation
starts.
