# Exchange source contract review

Reviewed source baseline: `5a47981a6aade0e7a0410f9f3d8c4f237c6bd327`.
Source hardening commit: `ab20ed6abe5c254a9827d78709c5b3a1ef23c56e`.
This work remains on `research/unified-exact-arb-graph`; no merge, deployment,
trading credentials, real orders or execution authority are changed by this review.
The frozen champion economics are outside this patch.

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
- The SOTA launcher child-count assertion now reads the canonical process manifest
  instead of assuming a historical literal count. The equality check remains.
- The dedicated native review configures the test-enabled CMake tree. The
  deployment-only configuration cannot be combined with BUILD_TESTING=ON.
  Test dependencies are installed; no CMake safety assertion was weakened.

## Reproducible validation checkpoint

Validated code SHA: `32b5eae22290b03d5c5d825573c05c22e9859957`.
Workflow: `V7 exact-arbitrage review`.
GitHub Actions run: `35891257958`, completed successfully at
`2026-09-23T16:49:43Z` on Ubuntu 24.04 runners.

- Python job `107284000589`: 28 standalone source-contract tests PASS and
  72 graph/adversarial/universe/warm-screen/hotset/observer/manifest/monitoring
  tests PASS. These are two separate test commands, 100 Python tests in total.
- Native Release job `107284000749`: PASS.
- Native Debug job `107284000807`: PASS.
- Native ASan/UBSan job `107284001006`: PASS.

Each native job builds the observer and the four selected graph, PureArb lane,
PureArb replay-parity and PureArb multi-engine test targets, then runs the selected
CTest group. This is a scoped review matrix, not a claim that every repository
workflow, every economic property, or a London deployment has passed.

A separate local rerun of the 28 source tests passed on Python **3.13.5**.
The earlier report's local Python 3.11 label was incorrect. Local validation used
only the reconstructed source/test files; it was not a full repository build.

Run evidence:
https://github.com/ENRICOBIGNOZZI/Polymarket/actions/runs/35891257958

Later commits on the active branch require their own checks. This document does
not transfer the checkpoint's green result to an untested moving branch head.

## Evidence boundary and remaining acceptance requirements

- Independent NegRisk membership/settlement attestations are still required
  before Gamma-derived non-binary candidate groups may become enabled relations.
- REST warm-screen apparent edges remain NONATOMIC_PUBLIC_REST_SCREEN_ONLY.
  They are not fills, executable arbitrage, or realized PnL.
- This source patch handles refresh errors; consumer-side source-age gates are
  a separate requirement when a collector stops without publishing an error.
- Broad causal subscription coverage, current live relation yield, controlled
  champion latency non-regression, the full repository release gates and London
  PAPER observation are not established by the tests listed above.
- PR #1470 remains a research review, not an execution-authority promotion.
  No PROMOTABLE artifact or successful London cutover is asserted here.

Protocol references reviewed on 2026-09-23:
https://docs.polymarket.com/concepts/negative-risk
https://docs.polymarket.com/api-reference/events/list-events
