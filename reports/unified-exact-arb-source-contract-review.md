# Exchange source contract review

Reviewed baseline: `5a47981a6aade0e7a0410f9f3d8c4f237c6bd327`.
This change stays on the research branch. No champion economics, trading
credentials, real orders, deployment requests or execution authority are changed.

## Fixed

- Unknown/null/non-boolean fee flags no longer mean explicit zero fees.
  Contradictory flags and positive schedules invalidate fee metadata instead of
  falling back to fee-free. Nonzero rates require an explicit exponent.
- Observed Gamma NegRisk members no longer generate an unproven exhaustive
  equality. Even an apparently complete non-augmented list is retained as an
  `UNVERIFIED_CANDIDATE` until membership and economic semantics are independently
  attested. Missing augmentation flags remain unknown. Binary mapping evidence
  remains available but explicitly is not an independent on-chain attestation.
- Corrupt member arrays cannot silently drop an outcome; inconsistent market IDs
  or reused token bindings are quarantined instead of last-write-wins.
- Pagination error bodies and repeated/overlapping event pages fail closed.
  A bounded offset scan is not labelled an atomic point-in-time snapshot.
- A failed refresh atomically replaces the published universe with a safe empty
  source-invalid snapshot; an old successful universe is not kept active by the
  collector's error handler.

## Validation

28 standalone Python source-contract tests passed locally on Python 3.11.
They are deterministic fixtures; they are not current live venue evidence.
A read-only Linux workflow also runs these tests, existing graph/adversarial
Python tests, and native graph/champion/replay tests in Release, Debug and
ASan/UBSan. Its result must be inspected before claiming hosted validation.
The new workflow does not provide AWS credentials or a deployment action.

## Not resolved by this patch

- Independent NegRisk membership/settlement attestations and broad causal
  subscription integration remain required for enabled non-binary opportunities.
- REST warm-screen apparent edges remain NONATOMIC_PUBLIC_REST_SCREEN_ONLY.
  They are not fills, executable arbitrage, or realized PnL.
- Consumer-side source-age gating is still needed if the collector stops rather
  than executing its refresh error handler.
- Full Linux repository gates and London side-by-side non-regression validation
  are separate requirements. This patch does not authorize merge or promotion.

Protocol references reviewed on 2026-09-23:
https://docs.polymarket.com/concepts/negative-risk
https://docs.polymarket.com/api-reference/events/list-events
