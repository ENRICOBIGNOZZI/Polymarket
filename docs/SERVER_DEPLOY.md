# V7 private PAPER server deployment

The production PAPER node is private behind Tailscale. The canonical remote checkout is `~/polymarket`; deployment is pinned to `paper-validated`.

## Runtime services

On macOS, launchd keeps these services alive:

- `com.polymarket.awake` — prevents idle sleep;
- `com.polymarket.paper` — runs `scripts/paper_v7_loop.sh config/paper_v7.json runs/paper_v7_live`;
- `com.polymarket.exporter` — runs `monitoring/exporter_latest_v7.py` on `127.0.0.1:9108`;
- `com.polymarket.prometheus` — Prometheus on `127.0.0.1:9090`;
- `com.polymarket.grafana` — Grafana on `127.0.0.1:3000`.

There is no runtime-version selector. The service definition starts V7 directly.

Linux deployment uses the equivalent `polymarket-paper.service` and `polymarket-monitoring.service` systemd units.

## First bootstrap

macOS:

```bash
cd ~/polymarket
bash ops/bootstrap_macos.sh
```

Linux:

```bash
bash ops/bootstrap_server.sh
```

Both bootstraps require the checked-out live champion to be exactly V7, build/test the V7 repository, and install only V7 runtime/monitoring services.

## Grafana

Grafana remains bound to loopback. On the canonical macOS server, `ops/apply_runtime_config_macos.sh` publishes it privately through Tailscale Serve. The operator URL is:

```text
http://mamma-portfolio.tail1bae85.ts.net
```

The Tailscale DNS name and Serve route are verified before Grafana configuration is accepted.

## Safe updates

The deployed revision comes from `paper-validated`, not from an arbitrary `main` checkout.

macOS:

```bash
cd ~/polymarket
bash ops/update_server_macos.sh
```

Linux:

```bash
cd ~/polymarket
bash ops/update_server.sh
```

The macOS updater serializes checkout mutation with a deployment mutex, validates the exact candidate in an isolated worktree, builds/tests V7, requests a bounded runtime-owner handoff, restarts services and waits for V7 runtime health. A failed candidate is rolled back to the previous checkout/build after diagnostics are captured.

## GitHub Actions deployment

`.github/workflows/deploy-paper-server.yml` is the only registered deploy authority. It runs after a successful `V7 live PAPER smoke` or on its registered reconciliation schedule when enabled.

The workflow proves that:

- `paper-validated` is on the current `main` lineage;
- a workflow-triggered deploy matches the exact validated SHA;
- `config/live_champion.json` selects V7;
- the deployed checkout becomes exactly `paper-validated`;
- exporter health is green;
- V7 runtime/allocator/strategy/proxy files are present and valid;
- drawdown is within the 15% hard limit;
- Prometheus and Grafana are healthy;
- the expected launchd/systemd services are active.

Repository credentials such as the SSH private key and Tailscale credentials remain outside version control.

## Health workflow

`.github/workflows/server-health.yml` is read-only. It verifies the deployed revision and V7 runtime invariants but does not repair code or reinterpret operator policy.

Useful local checks:

```bash
curl -fsS http://127.0.0.1:9108/healthz
curl -fsS http://127.0.0.1:9108/metrics | grep '^polymarket_'
curl -fsS http://127.0.0.1:9090/-/ready
curl -fsS http://127.0.0.1:3000/api/health
```

On macOS:

```bash
bash ops/macos_service_control.sh status
```

Authenticated execution is disabled and no deployment/bootstrap service starts a real-money executor.
