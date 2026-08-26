# Private V7 PAPER server deployment

The private server deploys only the exact revision referenced by `paper-validated`. The canonical runtime is V7 and authenticated order submission is disabled.

## Runtime services

The macOS node keeps these services alive across reboots:

- `com.polymarket.awake` — prevents idle system sleep;
- `com.polymarket.paper` — starts `scripts/run_paper.sh`;
- `com.polymarket.exporter` — V7 metrics on `127.0.0.1:9108`;
- `com.polymarket.prometheus` — Prometheus on `127.0.0.1:9090`;
- `com.polymarket.grafana` — Grafana on `127.0.0.1:3000`.

`config/live_champion.json` must select V7, `scripts/paper_v7_loop.sh`, `config/paper_v7.json` and `runs/paper_v7_live`. Deployment fails closed if the validated revision does not satisfy that manifest contract.

## First bootstrap

The repository must already exist at `~/polymarket` on the server. From that checkout:

```bash
git fetch origin main paper-validated
bash ops/bootstrap_macos.sh
```

Bootstrap installs required dependencies, builds the maintained binaries, runs deterministic tests, installs service definitions and configures local monitoring.

## Safe update

```bash
cd ~/polymarket
bash ops/update_server_macos.sh
```

The updater resolves the exact validated revision, builds and tests it in isolation, performs the runtime handoff under the singleton contract, updates the production checkout/build and verifies health. Failed post-deploy verification does not silently accept a different revision.

## GitHub Actions deployment

`.github/workflows/deploy-paper-server.yml` is the only deployment authority in the registered control plane. It deploys only `paper-validated` and can be triggered by successful exact-SHA V7 PAPER validation or by its gated reconciliation schedule.

Required repository credentials/variables are kept outside version control. The workflow supports the configured Tailscale authentication mode and a dedicated SSH deployment key. Never commit private keys, Tailscale credentials or wallet/exchange credentials.

## Exact-SHA invariant

A deployment is valid only when:

```text
validated_sha == paper-validated == deployed HEAD
```

The deploy workflow also verifies that `paper-validated` is on the current `main` lineage and that the V7 manifest/config/runtime files belong to the same checkout. A successful SSH command or service restart alone is not deployment proof.

## Monitoring and health

After deployment, `.github/workflows/server-health.yml` verifies:

- exact deployed V7 SHA;
- one runtime owner and no duplicate state writers;
- recorder, broker and V7 market proxy liveness;
- canonical `runtime_status.json` state;
- PAPER-only and authenticated-execution-disabled boundaries;
- drawdown/kill-switch and execution health;
- V7 exporter and exact dashboard asset through the configured Tailscale route.

The canonical Grafana operator URL is:

```text
http://mamma-portfolio.tail1bae85.ts.net
```

## Local status

Typical server diagnostics:

```bash
sudo /usr/local/sbin/polymarket-service-control status
curl -fsS http://127.0.0.1:9108/metrics | grep '^polymarket_runtime_'
tail -f ~/Library/Logs/Polymarket/paper.out.log
tail -f ~/Library/Logs/Polymarket/paper.err.log
```

The deployment stack has no authenticated real-money execution path.
