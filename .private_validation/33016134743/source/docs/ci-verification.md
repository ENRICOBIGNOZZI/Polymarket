# CI Verification

This file exists to keep the live-data paper engine's CI validation explicit.

The `ci` workflow must pass both gates on every pull request:

1. C++20 configure/build plus deterministic unit and mock end-to-end tests.
2. A read-only live Gamma + CLOB smoke scan using `--scan-only` (no paper fills and no authenticated order submission).

A green workflow therefore validates the repository build and the public live-data integration, but it is not a real-money execution certification.
