# V7 Deployment

Deployment is a manual exact-SHA blue/green cutover. Automatic deployment and automatic champion promotion are disabled in `config/v7_scheduler_freeze.json`.

The candidate must be current `main`, pass exact-head CI/monitoring/single-writer validation, complete deterministic replay and bounded live-PAPER validation, then be explicitly advanced to `paper-validated`. Deployment requires the same explicitly approved SHA for `main`, `paper-validated` and the checked-out artifact.

Cutover order is stop new BLUE risk, cancel/drain, reconcile ledger/inventory/reservations, stop the BLUE writer, start GREEN, and verify market data, OMS, inventory, capital, risk, ledger, Grafana, alerts and identity.

Keep the prior exact artifact for rollback until operational stability is proven. Rollback never enables another architecture.
