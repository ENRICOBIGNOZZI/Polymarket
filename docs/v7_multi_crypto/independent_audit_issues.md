# Independent integration audit — 17 September 2026

This candidate integrates committed snapshots 38ee4495 and 6ae0e450 only.
Uncommitted work in the parallel worktrees is deliberately excluded.
The production checkout, processes, BTC protocol and canonical ledger are not modified.

## Defects fixed in the candidate

- AWS support AZ letters are account-specific. Compare physical zone IDs instead;
  retain the caller's own AZ name only as observed metadata.
- Regional reports must reject fractional/boolean/nonfinite counters and inconsistent
  totals. Invalid percentiles remain missing, not zero or an exception without JSON.
- The public clock probe can only GET the approved public `/time` endpoint. Validation
  CLI paths are tested as executables without network calls. Monotonic measured
  duration excludes warm-up; transport exceptions and closed-loop sampling remain explicit.
- Reconnect counting no longer subtracts the initial connection twice after warm-up.
- The feature test had an absolute path to a different development checkout. It now
  imports this checkout and asserts the imported module's actual path.
- Repeated identical shock snapshots reuse the original prior-sigma result. Conflicting
  duplicate versions and regressive snapshots are masked, not relearned.
- Oracle freshness is recomputed from observed receipt time and snapshot availability,
  not trusted from a cached `fresh` flag or cached age.
- Settlement-reference availability is recorded without backdating. A preceding
  observation is explicitly a proxy; only the exact boundary observation is a valid
  reference in this adapter. Missing provenance remains blocked.
- Derivative validity bits are applied individually. Missing field/age does not become
  a zero funding rate, open interest or age. Cached-source age is included.
- Crossed or invalid PM books cannot emit mid, spread or imbalance features.

The feature and oracle output schemas are version 2. Old version-1 shadow observations
are preserved but must not be concatenated with the repaired cohort as clean evidence.
The original frozen BTC execution path is unchanged, including its known limitations.

## Scope of the ledger audit

`scripts/v7_ledger_history_audit.py` uses the canonical parser and validates supplied
immutable ledger files. It reports duplicate IDs, missing receipts/links, open versus
reported terminal positions and the legacy recorded cash-fee identity. It never writes
an economic record or creates a payout. File hashes identify the input scope.

A matching recorded PnL identity is NOT verification of exchange fee incidence, public
resolution timing, simulated fills, full account cash, or all historical runs. Those
claims remain separate and are deliberately false/unknown in the report.

## Verification and promotion

The first combined native Release gate exposed the nonportable feature test: 196/197
CTest entries passed and that one failed. This failed receipt is retained; it is not
called a successful full verification. The repaired candidate must rerun all gates.

No new model, six-asset entry authority, cloud instance, real transaction, second
coordinator or writer is introduced. Local kernel benchmarks and public HTTP latency
cannot certify the entire signal-to-fill path or select a production region alone.

Rollback before deployment is simply not deploying this candidate. No live data or
positions have been migrated, deleted, rewritten or reset.
