# London PAPER migration runbook

This runbook prepares a London migration without changing the frozen BTC forward and without authorizing real execution.

## Safety boundary

The London generation remains `PAPER`/`SHADOW`: `paper_only=true`, `authenticated_execution=false`, `real_order_submission=false`, and no automatic cutover. Tailscale is for SSH, monitoring and administration only. The trading data path must use direct venue/Polymarket connectivity from the selected London host.

## Target topology

Benchmark three hosts in `eu-west-2` using the physical AZ IDs supplied by Polymarket support on 2026-09-16:

- `eu-west-2a` / `euw2-az2`
- `eu-west-2b` / `euw2-az3`
- `eu-west-2c` / `euw2-az1`

The names above belong to Polymarket's AWS account; they are not portable to another account. Each host verifies its own region and stable physical AZ ID through IMDSv2. Its account-local AZ name is recorded, not compared to Polymarket's letters. Use identical instance type, image, storage, exact Git SHA and benchmark load across the three hosts.

## Bootstrap

Run `ops/v7_london_bootstrap.sh` only on a fresh Ubuntu 24.04 EC2 host with `POLYMARKET_EXPECTED_SHA` set to the approved exact SHA. It installs build/runtime dependencies, builds Release, executes CTest and renders the existing systemd templates. The runtime units remain disabled after bootstrap.

Tailscale may then be authenticated separately for administration. Do not place Tailscale between venue feeds, the strategy and Polymarket.

## AWS provisioning

The repository now contains `infra/aws/v7_london_shootout.json` and `ops/v7_london_provision.sh`. The template creates one identical host in each physical AZ ID `euw2-az1`, `euw2-az2`, and `euw2-az3`, with encrypted gp3 storage, IMDSv2 required, zero inbound security-group rules, and an SSM instance role. Runtime services remain disabled after bootstrap.

From an AWS-authenticated operator shell:

```bash
export AWS_REGION=eu-west-2
export POLYMARKET_EXPECTED_SHA=$(git rev-parse HEAD)
./ops/v7_london_provision.sh
```

The wrapper verifies the AWS caller identity, maps the physical AZ IDs in the caller account, chooses the first configured compute instance type offered in all three zones, resolves the latest available Canonical Ubuntu 24.04 amd64 gp3 image, and writes a local provisioning receipt. It never authenticates Tailscale or enables the PAPER runtime.

SSM is the bootstrap administration path, so SSH port 22 does not need to be exposed. Tailscale remains an optional, separately authenticated admin path.

## Shootout

Use `ops/v7_london_benchmark.sh smoke` first. Smoke output is operational only and cannot select an AZ. After provisioning, the same three-host operation can be dispatched without inbound SSH:

```bash
python3 ops/v7_london_ssm_benchmark.py smoke --expected-sha "$POLYMARKET_EXPECTED_SHA"
```

Use `ops/v7_london_benchmark.sh formal` on all three hosts for the formal 24-hour HTTPS/TLS/TTFB probe. Gather the three probe JSON files and evaluate them with `scripts/v7_regional_shootout.py`, passing the three physical AZ IDs as candidate regions.

The evaluator requires the same exact SHA, sufficient duration and samples, bounded failure/reconnect rates, and valid percentile ordering. It never authorizes live execution or automatic cutover.

For WebSocket comparison, report connection health, reconnects, frame freshness and local jitter separately from one-way network latency. Exchange-to-host one-way latency is `UNKNOWN` unless comparable clock semantics support it; do not substitute RTT/2.

## Multi-path public-network evidence

`ops/v7_london_multipath_ssm.py` runs three versioned, read-only probes on each
London candidate host at one exact Git SHA:

- `v7_public_ws_latency_probe.py`: Polymarket public market WebSocket handshake,
  reconnect health and time to first book;
- `v7_external_tls_probe.py`: DNS/TCP/TLS connectivity to Polymarket and external
  crypto venues;
- `v7_external_ws_age_probe.py`: persistent public WebSocket feed timing for the
  external venues used by the research pipeline.

The combined receipt deliberately sets `selection_ready=false`,
`authenticated_order_latency_observed=false` and `one_way_latency_proven=false`.
Venue timestamp age is not interpreted as one-way latency without bounded clock
offset. These probes supplement the 24-hour HTTPS shootout; they cannot enable
PAPER runtime services, submit orders or select a production AZ automatically.

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

AWS resource creation is still blocked until an AWS-authenticated operator identity is available. The infrastructure definition, physical-AZ checks, common-instance-type selection, SSM administration path, bootstrap, benchmark orchestration, exact-SHA checks and migration contracts are now repository-complete; the blocker is account authorization, not missing provisioning code.

## Independent audit corrections

Public `/time` HTTPS ranking is not an end-to-end deployment decision. The
evaluator now explicitly reports `end_to_end_region_selection_ready=false`.
Comparable host hardware/image, binary and load manifests, persistent venue
WebSocket timing, clock uncertainty and signal-to-send measurements remain
required before choosing the production location. No host was provisioned by
this audit. IMDS requests are bounded and do not use a proxy.

AWS primary reference: https://docs.aws.amazon.com/ram/latest/userguide/working-with-az-ids.html
