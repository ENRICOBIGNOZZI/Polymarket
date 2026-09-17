# V7 crypto monitoring

Grafana is the operator view for the single PAPER crypto system. Prometheus exports the crypto-only runtime scope plus fail-closed SHA, single-writer, ledger, portfolio, crypto feed, execution, latency and safety metrics.

The Grafana surface is authoritative and intentionally small. Exactly three provisioned dashboards are allowed:

- `Polymarket V7 — Crypto PAPER Control Room`
- `Polymarket V7 — Crypto Runtime Latency`
- `Polymarket V7 — Crypto Settlement Evidence (ZERO AUTHORITY)`

They live in the `Polymarket V7 Crypto` folder and use the `Polymarket V7 Crypto Prometheus` datasource. The provider has `disableDeletion: false` and `allowUiUpdates: false`: a provisioned dashboard removed from the repository is deleted by Grafana, and UI edits cannot become a second source of truth.

No retired non-crypto algorithm has a visible panel, alert or navigation surface. Generic multi-engine UI such as the former engine-count/configured-engine wording and generic `arb events` series is forbidden. Crypto context coverage is shown as registered, zero-authority, model-registered and new-risk-authorized without presenting contexts as separate algorithms.

`python3 monitoring/validate_crypto_grafana.py --repository-root .` is the fail-closed deployment contract. It rejects extra dashboard JSON files, non-crypto titles/tags/navigation, stale multi-engine wording, retired generic arbitrage telemetry, a provider that would retain deleted dashboards, or an alert scope that expects anything other than the one canonical crypto algorithm.
