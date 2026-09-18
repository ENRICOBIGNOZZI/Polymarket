# London server access

Canonical AWS region: `eu-west-2` (London).

AWS IAM Identity Center / SSO region: `eu-north-1`.

Canonical AWS CLI profile: `polymarket-london`.

## 1. Normal AWS SSO access

Authenticate:

```bash
aws sso login --profile polymarket-london
```

Verify identity:

```bash
aws sts get-caller-identity --profile polymarket-london --region eu-west-2
```

List running London hosts:

```bash
aws ec2 describe-instances --profile polymarket-london --region eu-west-2 \
  --filters Name=instance-state-name,Values=running \
  --query 'Reservations[].Instances[].{Id:InstanceId,Name:Tags[?Key==\`Name\`]|[0].Value,AZ:Placement.AvailabilityZone,PrivateIP:PrivateIpAddress,Type:InstanceType}' \
  --output table
```

Check SSM reachability:

```bash
aws ssm describe-instance-information --profile polymarket-london --region eu-west-2 \
  --query 'InstanceInformationList[].{Id:InstanceId,Status:PingStatus,Platform:PlatformName}' \
  --output table
```

Open a shell:

```bash
aws ssm start-session --profile polymarket-london --region eu-west-2 --target INSTANCE_ID
```

For automation, prefer `aws ssm send-command` with `AWS-RunShellScript`; it leaves an auditable receipt.

## 2. Temporary AWS credentials

A temporary IAM Identity Center credential bundle can be used while valid. Never paste values into Git, scripts, shell history, PRs, issue comments, logs or chat output.

Load only into the current shell/process:

- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_SESSION_TOKEN`

Set `AWS_REGION=eu-west-2`, then verify with `aws sts get-caller-identity --region eu-west-2`.

If STS returns `ExpiredToken`, `InvalidClientTokenId` or equivalent, discard that session and use SSO. Do not create long-lived replacement credentials.

## 3. London topology

Polymarket requested order traffic from AWS `eu-west-2`.

Physical-zone mapping supplied by Polymarket:

- `eu-west-2a = euw2-az2`
- `eu-west-2b = euw2-az3`
- `eu-west-2c = euw2-az1`

Use physical Zone IDs for repeatable placement. Multiple London hosts may exist for benchmarking, but only one host may own the canonical PAPER runtime after cutover.

## 4. Do not confuse the existing SSH alias

The local SSH alias `polymarket` is a separate Tailscale machine/research workspace. It is not proof that you are on the AWS London production host.

Before deployment verify:

```bash
hostname
curl -s http://169.254.169.254/latest/meta-data/placement/region 2>/dev/null || true
```

and independently confirm the instance ID and `eu-west-2` placement through AWS.

## 5. Read-only London preflight

Before changing services:

```bash
hostname
date -u
systemctl show polymarket-v7-paper.service polymarket-v7-exporter.service \
  polymarket-v7-retention.timer grafana-server.service prometheus.service \
  -p Id -p ActiveState -p SubState -p UnitFileState --no-pager
df -h / /mnt/polymarket-data
```

Confirm the exact release SHA:

```bash
cat /home/ubuntu/polymarket-runtime/current/deploy/london/runtime_sha 2>/dev/null || true
```

No deployment is accepted from a dirty or mismatched checkout.

## 6. PAPER safety boundary

The London release must remain `paper_only=true`, `authenticated_execution=false`, `real_order_submission=false`, with one runtime owner, one capital/risk/OMS/inventory owner and one canonical ledger.

No private trading key is required for PAPER execution.

## 7. Post-cutover five-minute acceptance

For a full 300 seconds verify:

1. PAPER service remains active under the same PID/run/SHA.
2. Exporter `/healthz` remains healthy.
3. Prometheus scrapes the correct exporter target and data stays fresh.
4. Grafana datasource and provisioned crypto dashboards query that Prometheus.
5. Binance, Coinbase and Polymarket feed counters advance.
6. Evidence tapes grow; writer/error/drop/suppression counters remain clean.
7. Canonical ledger remains valid and single-writer.
8. No unexpected restart, KILL marker or disk-pressure event appears.
9. Missing telemetry remains unknown; it is never converted to zero.
10. Runtime remains PAPER-only throughout.

Use the repository/release-evidence read-only five-minute verifier only after healthy startup.

## 8. 24/7 operation

Only after five-minute acceptance succeeds: leave PAPER, exporter and retention supervision enabled; keep Grafana/Prometheus cold-plane; preserve restart limits and fail-closed kill behavior; keep immutable exact-SHA identity; retain/offload evidence without deleting unverified or open tapes.

If a critical invariant fails, stop accepting new PAPER risk and investigate. Never bypass release gates.
