# London PAPER migration runbook

This runbook prepares a London migration without changing the frozen BTC forward and without authorizing real execution.

## Safety boundary

The London generation remains `PAPER`/`SHADOW`: `paper_only=true`, `authenticated_execution=false`, `real_order_submission=false`, and no automatic cutover. Tailscale is for SSH, monitoring and administration only. The trading data path must use direct venue/Polymarket connectivity from the selected London host.

## Target topology

Benchmark three hosts in `eu-west-2` using the physical AZ IDs supplied by Polymarket support on 2026-09-16:

- `eu-west-2a` / `euw2-az2`
- `eu-west-2b` / `euw2-az3`
- `eu-west-2c` / `euw2-az1`

Every host must verify its observed EC2 AZ name and AZ ID through IMDSv2 before a probe is accepted. Use identical instance type, image, storage, exact Git SHA and benchmark load across the three hosts.

## Bootstrap

Run `ops/v7_london_bootstrap.sh` only on a fresh Ubuntu 24.04 EC2 host with `POLYMARKET_EXPECTED_SHA` set to the approved exact SHA. It installs build/runtime dependencies, builds Release, executes CTest and renders the existing systemd templates. The runtime units remain disabled after bootstrap.

Tailscale may then be authenticated separately for administration. Do not place Tailscale between venue feeds, the strategy and Polymarket.

## Shootout

Use `ops/v7_london_benchmark.sh smoke` first. Smoke output is operational only and cannot select an AZ.

Use `ops/v7_london_benchmark.sh formal` on all three hosts for the formal 24-hour HTTPS/TLS/TTFB probe. Gather the three probe JSON files and evaluate them with `scripts/v7_regional_shootout.py`, passing the three physical AZ IDs as candidate regions.

The evaluator requires the same exact SHA, sufficient duration and samples, bounded failure/reconnect rates, and valid percentile ordering. It never authorizes live execution or automatic cutover.

For WebSocket comparison, report connection health, reconnects, frame freshness and local jitter separately from one-way network latency. Exchange-to-host one-way latency is `UNKNOWN` unless comparable clock semantics support it; do not substitute RTT/2.

## Data migration

Do not copy the current `runs/paper_v7_live` tree wholesale. Do not reuse the Mac canonical ledger.

Copy only explicitly required immutable/durable material: trained model artifacts, durable datasets needed for replay/training, registries, required research artifacts and approved configuration. Record checksums and source paths for every copied object.

London must start a new run ID, a new ledger ID and a new ledger generation. Old positions remain owned by the old generation until their settlement lineage is safely completed or explicitly migrated by a tested procedure.

## Cutover

The cutover invariant is one canonical writer at all times.

1. Keep London PAPER-disabled while bootstrap and shootout run.
2. Select the measured AZ only after all three formal probes are valid.
3. Validate exact SHA, models, feed health, local PM book, coordinator, ledger and accounting on London in zero-real-authority mode.
4. Put the Mac generation into controlled drain: block new entries and keep reconciliation/settlement available.
5. Seal the old writer and record the final ledger/run identity. Never fabricate flat inventory or zero payout to pass the gate.
6. Start the London PAPER supervisor with a fresh run/ledger generation.
7. Verify one canonical writer, advancing feeds, correct mappings, PAPER authority flags and accounting before declaring the cutover complete.

## GitHub Actions

The deploy workflows already read `POLYMARKET_SERVER_HOST`, `POLYMARKET_SERVER_USER` and `POLYMARKET_SERVER_PORT`. Do not change those variables before the selected London host passes PAPER validation. After cutover, set the host to the selected London Tailscale address and keep Tailscale limited to deployment/admin traffic.

## Current blocker

AWS provisioning is not currently possible from the existing Mac environment: no usable AWS CLI/account identity was found during the 2026-09-16 audit. Provisioning of the three EC2 hosts therefore remains blocked on explicit AWS access. The repository-side bootstrap, benchmark, exact-SHA checks and migration contracts can be completed independently.
