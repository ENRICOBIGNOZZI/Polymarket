# V7 Deployment

Deployment is a manual exact-SHA blue/green cutover. Automatic deployment and automatic champion promotion are disabled in `config/v7_scheduler_freeze.json`.

The candidate must be current `main`, pass exact-head CI/monitoring/single-writer validation, and complete deterministic replay plus bounded live-PAPER validation. Deployment accepts only an explicitly approved 40-character SHA that is still the exact tip of `main`; the checked-out artifact and runtime identity must match that SHA.

Rollback is an explicit operator-approved `git revert` on `main`, followed by the same exact-SHA validation and deployment flow. This restores the prior tree without introducing a mutable secondary deployment branch or bypassing current-main checks.

Repository protection is defined as an API-ready GitHub ruleset in `artifacts/github_main_ruleset.json`. Apply it with an authenticated owner token using `POST /repos/ENRICOBIGNOZZI/Polymarket/rulesets`; it requires pull requests, linear history, review resolution, and the uniquely named V7 Release, Debug, monitoring, and single-writer checks. Exact-SHA PAPER validation and deploy remain post-merge manual gates because they validate the immutable `main` commit itself.

Cutover order is stop new BLUE risk, cancel/drain, reconcile ledger/inventory/reservations, stop the BLUE writer, start GREEN, and verify market data, OMS, inventory, capital, risk, ledger, Grafana, alerts and identity.

Keep the prior exact artifact for forensic comparison until operational stability is proven. Rollback never enables another architecture.

An existing ledger containing another SHA is never reused. Cutover first seals
and archives that ledger generation, then starts a new exact-SHA run identity;
the supervisor quarantines mixed-SHA evidence instead of silently appending.
