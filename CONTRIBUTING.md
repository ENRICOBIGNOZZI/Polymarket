# Contributing to Polymarket V7

V7 is the only supported architecture and runtime. Contributions must finish or
harden V7; do not add another numbered generation, runtime owner, ledger,
capital allocator, risk engine, PAPER loop, or deployment path.

## Change requirements

1. Start from the current exact `main` SHA and keep the change narrowly scoped.
2. Preserve the PAPER-only safety boundary and the single execution writer.
3. Keep market, receive, decision, and execution timestamps causal.
4. Identify economically meaningful output with canonical run, dataset, model,
   universe, configuration, binary, and build hashes.
5. Add deterministic tests for the changed contract.
6. Run `./scripts/verify_v7.sh` before requesting review. Set
   `V7_VERIFY_SANITIZERS=1` for ASan/UBSan and `V7_VERIFY_TSAN=1` for the
   practical thread-sanitizer pass when changing native concurrency code.

Do not commit build output, runtime state, datasets, credentials, local
configuration, or generated `runs/` content. Dataset and run manifests are
immutable: create a new identity when any input changes rather than overwriting
an existing manifest.

## Review expectations

Pull requests should state the exact base/head SHAs, affected V7 capability,
tests run, PAPER safety impact, provenance impact, and any remaining
uncertainty. Correctness, causality, execution realism, and reproducibility are
reviewed independently from claimed economic performance.
