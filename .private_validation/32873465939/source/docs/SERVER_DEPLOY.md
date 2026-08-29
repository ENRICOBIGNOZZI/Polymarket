# Private paper-live server deployment

The production paper node is intentionally private behind Tailscale. The current host is a macOS machine reached as `enrico@100.104.183.109:22`.

## Runtime model

The machine keeps these launchd services alive across reboots:

- `com.polymarket.awake` — prevents idle system sleep;
- `com.polymarket.paper` — starts `scripts/paper_latest_loop.sh`;
- `com.polymarket.exporter` — canonical latest-runtime metrics on `127.0.0.1:9108`;
- `com.polymarket.prometheus` — local Prometheus on `127.0.0.1:9090`;
- `com.polymarket.grafana` — local Grafana on `127.0.0.1:3000`.

`paper_latest_loop.sh` chooses the numerically newest committed `paper_vN_loop.sh` with a matching `config/paper_vN.json`, so V5/V6 do not require a new service definition.

Monitoring is native on macOS (Homebrew Grafana + Prometheus) rather than Docker. Grafana remains bound to localhost and should normally be viewed through an SSH tunnel.

## First bootstrap

The repository must already exist at `~/polymarket` on the server. Then from the server checkout:

```bash
git fetch origin main
git reset --hard origin/main
bash ops/bootstrap_macos.sh
```

The bootstrap installs Homebrew dependencies, builds Release, runs deterministic tests, installs launchd services, creates a random Grafana admin password, and verifies exporter/Prometheus/Grafana health.

The password is stored only on the server at:

```text
~/.config/polymarket/grafana-admin-password
```

## Local Grafana access

From a trusted Mac already on the tailnet:

```bash
ssh -N -L 3000:127.0.0.1:3000 polymarket
```

Then open `http://127.0.0.1:3000` locally.

## Safe updates

After first bootstrap:

```bash
cd ~/polymarket
bash ops/update_server_macos.sh
```

The updater fetches `origin/main`, validates the candidate commit in an isolated worktree, builds/tests it, swaps the production build, restarts services and performs health checks. A failed post-deploy health check rolls the checkout/build back to the prior commit.

## GitHub Actions deployment

`deploy-paper-server.yml` can update the server automatically after a successful merge to `main`. It requires:

Repository secrets:

- `TS_AUTHKEY` — a Tailscale auth key allowed to reach the server;
- `POLYMARKET_SERVER_SSH_KEY` — a dedicated SSH deployment private key.

Repository variables:

- `POLYMARKET_SERVER_DEPLOY=true` to enable deployment on pushes to `main`;
- optional `POLYMARKET_SERVER_HOST`, `POLYMARKET_SERVER_USER`, `POLYMARKET_SERVER_PORT` overrides.

The matching deployment public key must be present in `~/.ssh/authorized_keys` for the server user. Never commit private keys or Tailscale auth keys.

`paper-server-health.yml` performs an hourly private health check through Tailscale and records server commit, service state, canonical runtime metrics, drawdown, kill switch, execution imbalance and OOS gate state.

## Status and logs

```bash
sudo /usr/local/sbin/polymarket-service-control status
curl -fsS http://127.0.0.1:9108/metrics | grep '^polymarket_runtime_'
tail -f ~/Library/Logs/Polymarket/paper.out.log
tail -f ~/Library/Logs/Polymarket/paper.err.log
```

The real-money pilot remains separate and is never started by launchd, CI, the paper loop, deployment, or health-check workflows.
