# POLYMARKET V7 — UNIFIED ECONOMIC SYSTEM, SECURITY RECOVERY, EXACT-SHA PAPER CUTOVER, AND FINAL LEGACY ERADICATION

## AUTONOMOUS IMPLEMENTATION DIRECTIVE

Repository: `ENRICOBIGNOZZI/Polymarket`

Baseline reviewed before this directive: `main@8d9e8e603aae4d73842212eedcf8e0e06383127f`

Target architecture: **V7 only**

Execution mode throughout this directive: **PAPER / SHADOW only**

Primary mission:

> Convert the repository from a collection of historically separate sleeves, launchers, compatibility paths, and observers into one coherent V7 trading system with one economic authority, two initial decision engines, one global portfolio/risk/execution stack, and a zero-authority research data plane. Only after exact-SHA equivalence, forward PAPER validation, rollback proof, and security remediation are complete, remove all active legacy code, configuration, workflows, documentation, branches, schemas, and runtime paths that are no longer required.

This is not a request for another bot, a V8, a rewrite for aesthetic reasons, or premature live trading. It is a controlled convergence and evidence program.

---

# 1. CURRENT VERIFIED STARTING POINT

Treat the following as the initial hypothesis and verify every item again from the repository, GitHub Actions, and the deployed PAPER host before modifying code.

1. `main` currently points to `8d9e8e603aae4d73842212eedcf8e0e06383127f`.
2. V7 Debug, Release, static-contract, sanitizer, monitoring, and private-runtime single-writer checks have recently passed on that SHA.
3. The aggregate CI is **not green**. The exact-SHA `security-audit-v7` job fails because the full-history scanner reports one `assigned_secret` candidate at historical location:
   - commit prefix: `7e699ba15770`
   - path: `docs/MONITORING.md`
   - line: `52`
   - do not print, recover, copy, or expose the detected value.
4. A recent `V7 live PAPER validation` workflow may display `success`, but its substantive PAPER, safety, evidence, and deployment steps were skipped when exact-SHA CI was unavailable or red. A skipped validation is not a successful validation.
5. There are currently no open pull requests, but there are two non-main Codex branches in addition to `main`:
   - `codex/v7-maker-ttl`
   - `codex/v7-market-ws-envelope`
6. `main` is not protected and the repository currently has no ruleset.
7. The repository already contains the correct architectural direction:
   - `BTC_SETTLEMENT_ENGINE`
   - `STRUCTURAL_ARB_ENGINE`
   - one canonical OMS, inventory, risk, capital allocator, and ledger
   - research families with zero independent execution authority.
8. `professional_maker`, `crypto_informed_taker`, and `crypto_settlement_fair` are components of the same BTC settlement decision engine. They are not three independent capital owners or three independent bots.
9. `hard_arb` and `fast_structural` are components of the same structural-arbitrage decision engine. They are not independent execution stacks.
10. Current documentation and orchestration remain partially inconsistent with that architecture. In particular, inspect at least:
    - `README.md`
    - `config/operator_directives.json`
    - `config/paper_v7.json`
    - `config/v7_btc_settlement_engine.json`
    - `config/v7_strategy_registry.json`
    - `scripts/paper_v7_execution_loop.sh`
    - `ops/update_server_v7.sh`
    - all monitoring exporters, systemd definitions, workflows, tests, and schema readers.
11. The active shell loop launches many fault-isolated processes. Multiple processes are allowed; multiple economic authorities are not. Do not confuse “one system” with “one PID”.
12. Existing repository-convergence audit artifacts may be provenance-bound to an older SHA. Regenerate them for the final exact current SHA rather than treating old audit claims as current truth.

Before doing anything else, write a redacted baseline report containing the verified values and discrepancies. Never place credentials, token values, private keys, signatures, access headers, passwords, or complete sensitive fingerprints in any public artifact.

---

# 2. NON-NEGOTIABLE INVARIANTS

These invariants override every local convenience and every older instruction found in the repository.

## 2.1 Safety and execution

- Remain `paper_only=true`.
- Remain `authenticated_execution=false`.
- Remain `real_order_submission=false`.
- Remain `real_capital_at_risk=false`.
- Do not enable automatic capital transfer.
- Do not enable automatic model promotion.
- Do not submit real orders, even for a canary.
- Do not use production credentials to test order submission.
- Read-only authenticated market-data access is allowed only through existing protected credential paths and only when no secret is logged or persisted.
- Any ambiguous state must fail closed to `CANCEL` and/or `NOTHING`, never to new risk.

## 2.2 Architectural authority

There must be exactly one canonical owner for each of the following:

- global portfolio decision;
- capital allocation;
- risk approval and kill state;
- OMS/order lifecycle;
- inventory/positions;
- append-only economic ledger;
- champion/candidate promotion decision;
- runtime identity and exact-SHA cutover.

No model, feature worker, collector, sleeve, observer, or component may own any of these independently.

## 2.3 Versioning

- Do not create V8.
- Do not create a second runtime.
- Do not create a second OMS.
- Do not create a second ledger.
- Do not create a second capital allocator.
- Do not fork a new “clean” repository to avoid migration work.
- Do not preserve obsolete behavior merely because tests encode it; determine whether the test protects a valid invariant or a legacy implementation detail.

## 2.4 Evidence and honesty

- Never claim profitability from technical readiness.
- Never claim deployment readiness from a skipped workflow.
- Never classify a model as mature because it can run.
- Keep technical readiness, feed readiness, model maturity, execution evidence, and economic readiness as separate states.
- No exploitation capital may be granted without mature terminal evidence, complete costs, positive conservative lower confidence bounds, observed executable capacity, and measured drawdown.
- Preserve causal receive-time ordering and exact settlement definitions.

## 2.5 Destructive work

- Do not begin final legacy deletion until the replacement is exact-SHA validated, forward-proven in PAPER, rollback-proven, and observed stable.
- Do not use indiscriminate `rm -rf`, broad glob deletion, or unreviewed history rewriting.
- Every deletion must come from a machine-readable classification manifest and have a reason, replacement, reference scan, test proof, and rollback pointer.
- Preserve immutable evidence required to audit prior PAPER runs and cutovers.

---

# 3. TARGET SYSTEM

The final system must implement this economic topology:

```text
READ-ONLY DATA SOURCES
  Polymarket market/private PAPER state
  Chainlink settlement/oracle data
  Binance / Coinbase / Bybit spot, perp, L2, trades and basis
  Kalshi / Limitless / other mapped prediction venues
  sports / OSINT / wallet / lifecycle / ranking / graph / PCA research inputs
                            |
                            v
                CANONICAL CAUSAL STATE PLANE
    receive timestamps, source health, provenance, mappings, staleness,
       immutable tapes, deterministic replay and feature contracts
                            |
                            v
                  RESEARCH / FEATURE PLANE
      versioned observations and models, zero execution authority
                            |
              +-------------+-------------+
              |                           |
              v                           v
      BTC_SETTLEMENT_ENGINE       STRUCTURAL_ARB_ENGINE
      - settlement fair value     - hard arb
      - informed taker            - fast structural
      - professional maker        - joint completion/unwind
      actions:                    actions:
      MAKE/TAKE/CANCEL/NOTHING    ARB/CANCEL/NOTHING
              |                           |
              +-------------+-------------+
                            v
             GLOBAL OPPORTUNITY / PORTFOLIO COORDINATOR
        compares conservative expected account-wealth changes,
       correlations, capacity, uncertainty, latency and inventory
                            |
                            v
                 ONE CANONICAL CAPITAL ALLOCATOR
                            |
                            v
                    ONE CANONICAL RISK ENGINE
                            |
                            v
                         ONE OMS
                            |
                            v
                   ONE INVENTORY / POSITION STATE
                            |
                            v
                    ONE APPEND-ONLY LEDGER
```

## 3.1 Meaning of “market maker is inside the system”

The professional market maker is not an independent strategy that must quote merely to remain active. It is an action-generation component of `BTC_SETTLEMENT_ENGINE`.

For each eligible BTC contract and state snapshot, the BTC engine must compare, on one common economic basis:

- passive bid/ask quote candidates;
- immediate taker candidates;
- cancellations/reprices;
- no action.

The decision must account for at least:

- fair-value interval, not only a point estimate;
- settlement probability and settlement-reference uncertainty;
- maker reach probability;
- conditional fill probability given reach;
- queue position or a conservative queue proxy;
- spread capture;
- verified rebate, with zero expected rebate if not authoritative;
- fill-conditioned adverse markout;
- cancel latency and stale-quote risk;
- inventory skew and liquidation/unwind cost;
- fees, slippage, capital cost and opportunity cost;
- executable depth/capacity;
- cross-venue/oracle staleness and disagreement;
- correlation with existing account risk.

A component may propose actions. Only the engine and then the global coordinator may select an opportunity. Only the canonical risk engine and OMS may authorize and represent an order lifecycle.

## 3.2 Standard opportunity contract

Converge engine output to a single typed, versioned `OpportunityEnvelope` or semantically equivalent contract. It must contain enough information to compare every action on one scale. At minimum:

- schema/version;
- exact `model_sha`, config hash, policy hash, run ID and source snapshot identity;
- engine ID and component provenance;
- market/event/contract identifiers and verified mapping identity;
- action type and side;
- decision receive timestamp and source event timestamps;
- fair value lower/point/upper;
- conservative expected net change in account wealth;
- complete cost vector and its authority status;
- uncertainty and calibration status;
- latency profile and economic percentile used;
- executable size/capacity and depth provenance;
- inventory and portfolio exposure deltas;
- settlement definition/source;
- reasons for eligibility or rejection;
- deterministic replay key;
- expiration/TTL.

Do not introduce this abstraction as a parallel path. Migrate existing candidates behind adapters, prove equivalence, switch the single consumer, and then remove adapters.

---

# 4. DEFINE LEGACY CORRECTLY

“Legacy” does not mean “anything old” and does not mean “all research”. Classify every tracked path, schema, runtime output, process, workflow, branch and tag into exactly one of these classes:

1. `KEEP_CANONICAL`
   - required by the final production/PAPER V7 core or by immutable audit evidence.
2. `MERGE_INTO_CANONICAL`
   - contains unique valid behavior that must be migrated into the unified core/engine.
3. `KEEP_ZERO_AUTHORITY_RESEARCH`
   - unique information or diagnostics, strictly unable to allocate capital, submit orders, write canonical PnL, or promote itself.
4. `KEEP_TEMPORARY_COMPATIBILITY`
   - needed only during migration, with an explicit deletion gate and maximum lifetime.
5. `ARCHIVE_HISTORY_ONLY`
   - useful for provenance or rollback but must not be imported, executed, deployed, monitored as current, or described as current architecture.
6. `DELETE_ACTIVE_LEGACY`
   - duplicate, dead, superseded, misleading, unsafe, or reachable only through obsolete compatibility behavior.
7. `EXTERNAL_BLOCKER`
   - cannot be completed without credential rotation, provider entitlement, GitHub-admin action, or physical host access.

Create a machine-readable manifest, for example:

`artifacts/v7_unification/path_classification.json`

Each entry must include:

- path or ref;
- object type;
- classification;
- current imports/callers/readers/writers;
- economic authority, if any;
- unique behavior/data contribution;
- canonical replacement;
- migration status;
- validation tests;
- deletion gate;
- rollback artifact/tag;
- final disposition;
- commit that completed disposition.

No path may be deleted solely because its name contains `legacy`, `old`, `v6`, `maker`, `graph`, or `research`. No path may survive solely because it is covered by a test.

---

# 5. EXECUTION PLAN

Work through the following phases in order. Small internal iterations are allowed, but do not skip phase gates.

## PHASE 0 — FREEZE, BASELINE, AND RECOVERABILITY

1. Fetch every remote ref and tag.
2. Confirm clean worktree and exact baseline SHA.
3. Create a dedicated migration branch from current `origin/main`; if `main` moved, re-audit the delta before proceeding.
4. Create immutable local and remote rollback references before destructive work:
   - signed/annotated tag where supported;
   - `git bundle --all` or equivalent offline mirror backup;
   - redacted checksum manifest.
5. Capture:
   - repository tree;
   - branches and tags;
   - workflows and required checks;
   - exact current CI results;
   - current deployed PAPER SHA and process identities;
   - active systemd units/process supervisors;
   - active canonical writers;
   - current open orders and PAPER positions;
   - current health/economic-readiness separation.
6. Generate `baseline.json`, `authority_graph.json`, and `runtime_process_graph.json` under a dedicated audit directory.
7. Do not copy runtime credentials into the repository or artifacts.

Gate: baseline is reproducible and rollback artifacts exist.

## PHASE 1 — P0 SECURITY RECOVERY

The full-history secret finding is the first blocker. Do not bypass it.

1. Reproduce both repository secret scanners using the exact CI commands and a full fetch.
2. Inspect the finding without printing the value. Record only redacted metadata necessary to classify it.
3. Determine whether it is:
   - a real credential/token/password/private key;
   - an expired but once-real secret;
   - a deterministic test fixture;
   - a false positive/example string.
4. If real or plausibly real:
   - assume compromise because the repository is public;
   - identify the provider without printing the value;
   - revoke/rotate the credential before considering history clean;
   - update the protected server-side credential file/environment through an owner-only path;
   - verify `0600` or stricter file permissions where applicable;
   - verify collectors remain read-only/PAPER;
   - prepare a controlled all-ref history rewrite using `git filter-repo` or an equally auditable tool;
   - preserve an offline pre-rewrite bundle;
   - produce a redacted old-to-new commit/tag mapping;
   - update or supersede cutover/provenance tags after the rewrite;
   - force-push only after all rewritten refs are locally scanned and validated.
5. If definitively false positive:
   - do not weaken global scanner rules;
   - add only a fingerprint/path/commit-scoped, documented exemption;
   - include a regression test proving other assignments remain detected;
   - keep the scanner fail-closed.
6. Run both history scanners over all refs and tags, not just the current tree.
7. Confirm no credential value appears in logs, workflow summaries, artifacts, test fixtures, documentation, shell history committed to Git, or generated patches.
8. Record a redacted `secret_remediation.json` with classification, actions, scanners and final result.

Gate: current-tree and full-history security jobs are green. If provider revocation requires an external account action unavailable to the agent, stop only this phase at a precise `EXTERNAL_BLOCKER`; do not pretend the repository is safe and do not continue to a deploy/cutover.

## PHASE 2 — COMPLETE AUTHORITY AND REACHABILITY AUDIT

Build a graph of every process and code path that can:

- create an executable candidate;
- allocate or reserve capital;
- approve risk;
- submit/cancel/replace an order;
- mutate inventory;
- write canonical ledger/PnL;
- change champion pointers;
- declare runtime health/readiness;
- initiate a cutover.

Use static search, runtime manifests, process inspection, tests, configs and deterministic traces. Do not rely on names.

Required proof targets:

- exactly one canonical capital owner;
- exactly one risk owner;
- exactly one OMS/order owner;
- exactly one inventory owner;
- exactly one canonical ledger writer;
- exactly one global portfolio coordinator;
- no research worker with executable authority;
- no shadow observer able to create or retain PAPER positions;
- no alternate script or workflow able to bypass the exact-SHA champion contract.

Flag all duplicate or implicit authority as P0/P1 defects.

Gate: machine-readable authority graph has no unexplained edge and tests fail when a second writer/owner is injected.

## PHASE 3 — NORMALIZE THE CANONICAL ARCHITECTURE

Implement the smallest architecture change that produces the target topology without discarding valid existing work.

1. Establish one canonical package/module boundary for:
   - causal state;
   - opportunity contract;
   - global portfolio coordination;
   - capital;
   - risk;
   - OMS;
   - inventory;
   - ledger;
   - exact-SHA runtime identity and cutover.
2. Establish two economic engine boundaries:
   - `BTC_SETTLEMENT_ENGINE`;
   - `STRUCTURAL_ARB_ENGINE`.
3. Route all engine proposals through the same global opportunity interface.
4. Remove any direct component-to-OMS, component-to-ledger, or component-to-capital shortcut.
5. Convert “strategy budgets” that actually refer to engine components into one of:
   - engine capital envelope;
   - component observation/information budget;
   - zero-authority research compute budget.
6. Ensure the global coordinator handles mutually exclusive and correlated opportunities rather than allowing independent sleeves to spend nominal budgets simultaneously.
7. Keep collectors fault-isolated. Do not force every network feed into a single process merely for conceptual purity.
8. Replace implicit file polling contracts with typed/versioned contracts only where this materially reduces ambiguity; do not perform an unbounded rewrite.

Gate: all executable decisions pass through the same coordinator/risk/OMS/ledger chain and deterministic replay reproduces decisions.

## PHASE 4 — FULLY INTEGRATE THE PROFESSIONAL MAKER

1. Eliminate the market maker as an independent economic authority.
2. Preserve maker-specific modeling as components within `BTC_SETTLEMENT_ENGINE`:
   - quote candidate generation;
   - reach estimation;
   - conditional fill estimation;
   - queue/priority model;
   - fill-conditioned markout;
   - cancel-latency/stale-risk model;
   - inventory-aware skew;
   - rebate/fee authority;
   - quote lifecycle diagnostics.
3. Place maker, taker and no-action candidates on the same conservative wealth-change objective.
4. Ensure risk actions preempt alpha actions.
5. Ensure external/oracle updates can cancel or reprice even without a Polymarket book event.
6. Require empirical latency artifacts for place ACK, cancel ACK, taker arrival and private confirmation; missing/immature profiles allow only `CANCEL/NOTHING`.
7. Separate these probabilities and measurements:
   - order was present;
   - aggressor reached price;
   - correct aggressor side;
   - order was fill-eligible;
   - queue was exhausted;
   - simulated/observed fill;
   - post-fill markout.
8. Do not tune the simulator to manufacture fills. Tune quoting policy only against causal forward evidence.
9. Keep maker observers zero-authority until the unified BTC engine has earned PAPER authority under the same economic gates.

Gate: there is no standalone maker capital/OMS/ledger authority, yet maker diagnostics and action selection remain at least behaviorally equivalent or demonstrably improved.

## PHASE 5 — STRUCTURAL ARBITRAGE UNIFICATION

1. Merge `hard_arb` and `fast_structural` decision ownership into `STRUCTURAL_ARB_ENGINE`.
2. Require full-depth executable books, joint completion logic, fee authority, partial-fill handling, timeout and unwind plans.
3. Represent multi-leg opportunities as one atomic economic intent even if execution is sequential.
4. Route all legs through the canonical OMS and ledger.
5. Prevent separate components from double-counting the same arbitrage or reserving capital twice.
6. Preserve near-miss evidence and opportunity-loss attribution without granting execution authority.

Gate: one structural-arbitrage owner, one capital reservation, one lifecycle, one terminal PnL unit.

## PHASE 6 — RESEARCH AND EXTERNAL DATA PLANE

Treat the following as possible information sources, not independent bots, unless future evidence justifies a new engine through a separate explicit governance decision:

- graph RV;
- micro-taker research;
- ranking;
- PCA/local factor;
- wallet intelligence;
- market-open/lifecycle;
- OSINT;
- sports latency;
- cross-platform mappings;
- Kalshi;
- Limitless;
- external crypto venues and derivatives features.

For every family:

1. Verify receive-time causality, source health and immutable tape semantics.
2. Verify market/entity/settlement mapping.
3. Declare feature schema and provenance.
4. Declare explicit zero authority for capital, OMS, inventory, ledger, orders and promotion.
5. Add source-level and feature-level ablations:
   - all sources;
   - minus one source;
   - stale/degraded source;
   - mapping uncertainty.
6. Retain a family only if it provides one of:
   - unique causal information;
   - necessary settlement truth;
   - execution/capacity/latency evidence;
   - materially useful operational diagnostics.
7. Archive/delete collectors that duplicate information without incremental value or create excessive operational risk.
8. A trading token must never be supplied to a collector that only requires public/read-only data.

Gate: every retained research process has a documented unique purpose and zero executable authority.

## PHASE 7 — SIMPLIFY ORCHESTRATION WITHOUT LOSING FAULT ISOLATION

`scripts/paper_v7_execution_loop.sh` is currently an operational concentration point. Refactor cautiously.

1. Build a declarative, versioned process manifest containing:
   - process ID/name;
   - executable and arguments;
   - owner class;
   - inputs/outputs;
   - restart policy;
   - liveness and freshness SLO;
   - exact-SHA/config identity;
   - authority flags;
   - dependencies;
   - drain behavior;
   - archival behavior.
2. Move repeated launch/status/restart logic into a typed supervisor or a minimal shared library.
3. Keep the top-level launcher small and auditable.
4. Preserve process isolation for feed failures.
5. Ensure a failed research collector cannot restart, stop, or mutate the canonical execution core.
6. Remove active knowledge of stale output paths after one successful migration cycle. In particular, eliminate permanent `legacy_stale_outputs` cleanup and `normalize_legacy_status`-style compatibility once no valid current artifact requires them.
7. Ensure cutover drain works for:
   - running executable PAPER cohort;
   - flat cohort;
   - never-started component;
   - zero-authority observer with no inventory file;
   - partial startup;
   - crashed/stale incumbent.
8. Make archive and promotion atomic and exact-SHA bound.

Gate: the launcher no longer encodes a historical catalogue of obsolete runtime outputs, and fault-injection tests prove safe isolation and restart behavior.

## PHASE 8 — ECONOMIC EVIDENCE AND CAPITAL COORDINATION

1. Keep the allocator evidence-based and fail-closed.
2. Require, per engine and action class where applicable:
   - mature terminal units;
   - complete fee/slippage/rebate/latency/unwind/capital costs;
   - positive day-block lower confidence bound;
   - positive PnL under at least 2x full-cost stress;
   - observed capital-hours;
   - observed executable capacity;
   - observed drawdown;
   - stable conditional calibration;
   - regime and source-health stratification.
3. Respect current settlement-model promotion floors unless a stronger, justified gate is implemented:
   - at least 30 settlement-labeled days;
   - at least 2,500 settlement-labeled contracts;
   - at least 300 forward-OOS policy trades;
   - sufficient conditional-calibration counts;
   - uncertainty below claimed edge.
4. Do not transfer exploitation capital automatically.
5. Keep exploration bounded and explicitly accounted for.
6. Attribute PnL by engine, action, component provenance, market, horizon, latency regime, fill path and cost component without creating multiple canonical ledgers.
7. Compare the unified decision policy against causal benchmarks:
   - Polymarket mid diagnostic;
   - oracle-only structural model;
   - external composite plus oracle;
   - settlement model;
   - settlement plus microstructure;
   - unified MAKE/TAKE/NOTHING policy.

Gate: technical readiness may be green while economic readiness remains red; the system reports that distinction correctly.

## PHASE 9 — EXACT-SHA SHADOW/PAPER VALIDATION AND CUTOVER

1. Require all exact-SHA checks to succeed:
   - Debug;
   - Release;
   - static V7 contract;
   - sanitizers;
   - security current-tree and full-history scans;
   - monitoring;
   - private-runtime single-writer;
   - deterministic replay;
   - migration/equivalence tests;
   - branch/ref hygiene.
2. Fix workflow semantics so a prerequisite-blocked or skipped PAPER validation cannot conclude as a misleading green success. Emit an explicit blocked/neutral/failure state that branch protection can interpret correctly.
3. Run a bounded exact-SHA public-data SHADOW/PAPER canary.
4. Verify the deployed SHA, config hash, policy hash, model hash, run ID, ledger ID and server ID.
5. Verify all real-order flags remain false.
6. Verify source freshness and degraded-source fail-closed behavior.
7. Verify exactly one canonical writer/owner for every authority.
8. Perform deterministic blue/green handoff:
   - freeze new risk;
   - drain/cancel incumbent;
   - prove flat orders/positions/inventory;
   - archive immutable incumbent state;
   - promote candidate atomically;
   - start exact target SHA;
   - verify fresh status and health;
   - retain rollback pointer.
9. Observe the target long enough to exercise normal updates, stale feeds, restarts, maker observers, structural opportunities and evidence writers. Do not use a mere startup proof.
10. Prove rollback by test and, where safe in PAPER, by controlled rehearsal.

Gate: full substantive PAPER validation ran and passed; no required step was skipped; target remains PAPER-only.

## PHASE 10 — FINAL LEGACY ERADICATION

Only now execute the deletion manifest.

For every `DELETE_ACTIVE_LEGACY` or completed `KEEP_TEMPORARY_COMPATIBILITY` item:

1. Confirm no current import, dynamic import, shell exec, workflow reference, systemd reference, config key, schema reader, monitoring panel, test fixture, documentation link, runtime reader/writer or deployment script uses it.
2. Confirm unique behavior has been migrated or intentionally rejected.
3. Confirm replacement tests pass.
4. Confirm an immutable rollback tag/bundle exists.
5. Delete the item in a small, reviewable commit.
6. Re-run the exact relevant tests after each deletion wave.
7. Record deletion reason, replacement, proof and commit in `legacy_deletion_manifest.json`.

Explicitly audit and remove or reclassify:

- V1–V6 current-tree code/config/workflows/docs that remain reachable or presented as current;
- alternate OMS, ledger, capital, risk or inventory paths;
- standalone maker/taker/graph/PCA/ranking/wallet execution authority;
- stale “one budget per strategy” assumptions for components;
- duplicate launchers and restart controllers;
- obsolete champion pointers;
- stale schemas and normalization adapters;
- stale runtime output cleanup lists;
- dead feature flags and fallback routes;
- old deployment scripts that can bypass exact-main-SHA gates;
- misleading README/architecture/operations documentation;
- generated files accidentally tracked as source;
- superseded tests that only preserve deleted implementation details;
- merged/superseded remote branches after comparison and archival;
- orphan tags only when they are not required for provenance; otherwise remap and document them.

Do not delete:

- immutable canonical ledger/evidence needed for audit;
- current cutover provenance;
- source tapes required to reproduce claimed evidence;
- valid research collectors with a documented unique contribution and zero authority;
- security remediation mapping/backups stored outside the public repository;
- rollback artifacts before the final system has been proven recoverable.

After the final deletion wave:

- `git grep`, import graph, process manifest, workflow scan and runtime inspection must show no active legacy dependency;
- the current tree must describe only the unified V7 architecture;
- all exact-SHA checks must pass again;
- repeat bounded PAPER validation and cutover proof.

Gate: no active legacy path remains, and historical/audit evidence is still reproducible.

## PHASE 11 — DOCUMENTATION, GOVERNANCE, AND FINAL STATE

1. Rewrite the README and architecture docs around:
   - one V7 system;
   - two initial economic engines;
   - one global coordinator/capital/risk/OMS/inventory/ledger chain;
   - market maker inside BTC engine;
   - zero-authority research/data plane;
   - technical versus economic readiness.
2. Replace stale `config/operator_directives.json` priorities with engine-centric policy. Components must not be described as independent capital strategies.
3. Regenerate the convergence audit for final exact SHA.
4. Generate a final SBOM/dependency and process manifest where supported.
5. Close/delete superseded branches only after `git log --left-right --cherry-pick` or equivalent proves no unique required work.
6. Establish repository governance:
   - protect `main`;
   - prohibit force-push and deletion except a documented emergency/history-remediation procedure;
   - require exact-SHA CI;
   - require security history scan;
   - require monitoring and private-runtime single-writer checks;
   - require substantive PAPER-validation status, not skipped-success;
   - require review for authority-contract changes;
   - use linear/squash history consistently.
7. Ensure branch-protection names match actual stable workflow check names.
8. Produce `FINAL_V7_UNIFICATION_REPORT.md` and a redacted machine-readable final attestation.

Gate: governance prevents reintroduction of duplicate authority, secrets, misleading skipped checks, or active legacy paths.

---

# 6. REQUIRED TESTS

Add or strengthen tests for all of the following. Prefer executable invariants over comments and substring-only tests.

## 6.1 Authority tests

- exactly one capital allocator;
- exactly one risk authority;
- exactly one OMS;
- exactly one inventory owner;
- exactly one canonical ledger writer;
- zero component/research independent authority;
- duplicate owner injection fails closed;
- stale/mismatched exact SHA fails closed.

## 6.2 BTC engine tests

- maker/taker/cancel/nothing evaluated on one objective;
- no maker action when uncertainty or latency invalidates edge;
- external/oracle update triggers cancel/reprice;
- missing latency artifact permits only cancel/nothing;
- fill probability is not conflated with reach probability;
- rebate is zero unless authoritative and executable;
- fill-conditioned markout and inventory costs enter economics;
- YES/NO complementarity and probability bounds hold;
- horizon-specific model artifacts cannot be mixed.

## 6.3 Structural-arbitrage tests

- full-depth and joint-completion requirements;
- no double capital reservation;
- partial fill and unwind accounting;
- multi-leg terminal PnL unit;
- fee/source/mapping uncertainty blocks execution.

## 6.4 Data/research tests

- receive-time causality;
- deterministic tapes and replay;
- source staleness and reconnect epochs;
- mapping confidence and settlement-definition matching;
- research worker cannot access executable APIs;
- read-only authenticated collectors never log credentials;
- source ablation is reproducible.

## 6.5 Cutover and operational tests

- flat incumbent;
- nonflat PAPER incumbent;
- partial startup;
- never-started component;
- zero-authority maker observer without state file;
- stale status;
- crashed writer;
- duplicate process;
- disk/write failure around atomic status/ledger updates;
- archive/promotion interruption;
- rollback to previous immutable champion.

## 6.6 Legacy-eradication tests

- no production import/exec/reference to deleted paths;
- no obsolete schema accepted after migration deadline;
- no old runtime output read or written;
- docs/configs contain no contradictory multi-bot authority description;
- orphan branches contain no unique required commits before deletion;
- current-tree and full-history secret scans green.

---

# 7. WORKING METHOD

1. Work autonomously and inspect before editing.
2. Do not ask for information that can be derived from code, Git history, CI, runtime state or existing protected configuration.
3. Stop only for a genuine external blocker such as provider credential revocation, unavailable entitlement, GitHub-admin control, or unreachable physical host. When blocked, provide exact evidence and the minimum external action required, then continue all independent work.
4. Use small, coherent commits with descriptive messages.
5. Keep `migration_manifest.json` and the audit artifacts updated in the same commit as each migration wave.
6. Do not hide failures by loosening assertions, reducing sample requirements, disabling workflows, changing a required check name, accepting stale schemas indefinitely, or marking a workflow successful after skipped substantive work.
7. Do not hard-code observed data as constants to make tests pass.
8. Do not fabricate economic samples, fills, capacity, PnL, latency, settlement labels or health.
9. Never echo secret values. Use environment-variable and protected-file names only.
10. Preserve bounded resource usage and production/PAPER observability.
11. Prefer removal over permanent compatibility once equivalence is proven.
12. Prefer one explicit typed contract over multiple implicit JSON dialects, but avoid rewriting stable components without a measurable architectural benefit.
13. Keep all real execution disabled at every commit.

At the end of every phase, print:

```text
PHASE:
BASE_SHA:
HEAD_SHA:
FILES_CHANGED:
INVARIANTS_PROVEN:
TESTS_RUN:
TEST_RESULTS:
RUNTIME_EVIDENCE:
ECONOMIC_EVIDENCE:
BLOCKERS:
ROLLBACK_POINTER:
NEXT_PHASE:
```

Do not report “done” until every final acceptance criterion below is satisfied or explicitly marked as an external blocker.

---

# 8. FINAL ACCEPTANCE CRITERIA

The mission is complete only when all applicable statements are true:

## Security

- full-history and current-tree secret scans are green;
- any real exposed credential was rotated/revoked before history was declared clean;
- no secret was printed or committed during remediation.

## Architecture

- exactly one V7 economic system exists;
- exactly two initial economic engines exist: BTC settlement and structural arbitrage;
- professional maker is an internal BTC-engine component/action generator;
- hard arb and fast structural share one structural engine;
- all opportunities pass through one global portfolio coordinator;
- one capital allocator, risk engine, OMS, inventory and ledger exist;
- no duplicate or bypass authority remains.

## Research

- retained research/data families are versioned, causal, mapped, health-monitored and zero-authority;
- obsolete or valueless duplicate collectors are archived/deleted;
- no research worker can submit or represent executable orders.

## Validation

- exact-SHA Debug, Release, sanitizer, static, monitoring, single-writer, security and migration checks pass;
- substantive exact-SHA PAPER validation actually ran; it was not skipped;
- deterministic replay and blue/green cutover pass;
- rollback is proven;
- deployed runtime identity is fresh and exact.

## Economics

- technical readiness and economic readiness remain separate;
- no strategy/engine receives exploitation capital without required evidence;
- costs, capacity and drawdown are observed and conservative;
- no unsupported profitability claim appears in code, docs or reports.

## Legacy

- every tracked path/ref/schema/runtime output is classified;
- all `DELETE_ACTIVE_LEGACY` items are removed;
- temporary compatibility is removed after its gate;
- current code/workflows/docs have no active dependency on legacy paths;
- stale multi-sleeve economic-authority language is gone;
- immutable evidence and rollback provenance remain available.

## Governance

- `main` is protected by the correct required checks;
- skipped validation cannot appear as green readiness;
- authority-contract changes require review;
- obsolete branches have been compared, archived where necessary, and removed;
- final convergence audit is bound to the final exact SHA.

Final completion output must include:

1. final SHA and parent baseline;
2. concise architecture diagram;
3. exact authority counts;
4. retained research families and reasons;
5. files/schemas/workflows/branches deleted or archived;
6. security remediation result, redacted;
7. exact test/workflow results;
8. PAPER cutover and rollback proof;
9. technical readiness state;
10. economic readiness state with evidence counts;
11. remaining external blockers, if any;
12. explicit statement that authenticated execution and real order submission remain disabled.

---

# 9. FIRST ACTIONS TO EXECUTE NOW

Begin immediately with these actions, adapting exact commands to the repository and its CI definitions:

1. `git fetch --all --tags --prune`.
2. Record `git status`, `git rev-parse HEAD`, `git rev-parse origin/main`, branches, tags and worktrees.
3. Re-run exact CI commands from `.github/workflows/ci.yml`, especially both full-history secret scanners.
4. Generate the baseline and authority graphs.
5. Classify the historical secret finding without exposing its value.
6. Audit `README.md`, `config/operator_directives.json`, `config/paper_v7.json`, `config/v7_btc_settlement_engine.json`, `config/v7_strategy_registry.json`, `scripts/paper_v7_execution_loop.sh`, all workflows and all deployment/systemd files for contradictions with the target architecture.
7. Build the complete path/ref/schema/runtime classification manifest.
8. Do not begin final deletion until Phases 0–9 have passed.

The desired final outcome is not “less code” in isolation. It is a smaller, auditable, causally correct, exact-SHA-reproducible V7 system in which every retained line either contributes to data truth, decision quality, risk control, execution, evidence, observability or recovery—and nothing obsolete can still affect the active runtime.
