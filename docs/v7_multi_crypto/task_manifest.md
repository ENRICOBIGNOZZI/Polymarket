# V7 Multi-Crypto Lead/Lag — Task Manifest

Status below separates repository completion from deployment/economic evidence.
No row grants real-money authority.

| Milestone | Repository state | Remaining external/evidence gate |
|---|---|---|
| M0 Audit and integrity | VERIFIED_CODE | Historical/live ledger evidence is only complete for supplied immutable inputs; no active production checkout is attached here. |
| M1 Core common | VERIFIED_CODE | ContractState, settlement/rules bindings and shared six-asset registry are integrated. Runtime deployment still required. |
| M2 Data plane | VERIFIED_SHADOW_CODE | Six-asset venue, PM BookHub, OracleHub, feature/label planes and one zero-authority SHADOW supervisor are integrated; the candidate itself is not deployed. |
| M3 Execution/accounting | VERIFIED_CODE | PAPER activation still requires a frozen eligible cohort and explicit coordinator authorization. |
| M4 Speed | VERIFIED_INTERNAL | L1 latency/capacity replay and synthetic internal gates pass; venue/network/end-to-end London latency is not yet measured. |
| M5 Research | VERIFIED_PIPELINE / INSUFFICIENT_EVIDENCE | Shock calibration, repricing research and fail-closed readiness gates are implemented; independent economic evidence is still insufficient. |
| M6 Multi-crypto forward | IMPLEMENTED / BLOCKED_EVIDENCE | Freeze/report/reservation/execution mechanics exist; ETH/SOL PAPER remains off until prospective evidence gates pass. |
| M7 Breadth | READY_SHADOW | XRP/DOGE/BNB M5/M15 remain zero-authority SHADOW. |
| M8 Capacity/readiness | VERIFIED_CODE / NO_PROMOTION | Risk/correlation/capacity tooling exists; no real-money promotion is authorized. |
| M9 London regional migration | READY_TO_PROVISION / BLOCKED_AWS_ACCESS | Provisioner/bootstrap/shootout/runbook exist; this Mac has no discoverable AWS credentials and no launch network/admin parameters. |

## Non-negotiable invariants

- Frozen BTC behavior is not silently redefined.
- `paper_only=true`.
- `authenticated_execution=false`.
- `real_order_submission=false`.
- `real_capital_at_risk=false`.
- No automatic promotion or automatic cutover.
- One global coordinator/risk/execution authority and one canonical ledger writer.
- Missing market/rules/feed/oracle/fill/settlement information remains unknown or blocked, never zero evidence.

## London provisioning boundary

`scripts/v7_london_provision.py` is the only repository-side host creation path.
Default mode is plan-only and has no AWS side effect. `--apply` requires:

- authenticated AWS identity in `eu-west-2`;
- exactly one subnet for each approved physical AZ ID (`euw2-az1/2/3`);
- one security group in the shared VPC;
- an explicit admin path (`key_name` or IAM instance profile);
- an exact 40-character candidate SHA.

The provisioner selects one instance type available in all three physical zones,
resolves the current Canonical Ubuntu 24.04 gp3 AMI through SSM, enforces IMDSv2,
launches exactly one host per physical zone, and writes a PAPER-only receipt.
It does not start the runtime and cannot cut over automatically.

After provisioning, the mandatory order is: bootstrap -> smoke -> 24h formal
three-AZ shootout -> measured selection -> zero-real-authority London validation ->
controlled single-writer PAPER cutover. No step may be skipped because public HTTP
RTT or geography appears favorable.
