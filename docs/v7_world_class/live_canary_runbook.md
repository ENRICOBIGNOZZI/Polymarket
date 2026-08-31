# V7 live-canary runbook

This repository cannot start a real canary. Its orchestrator is a validation
only tool and always stops at `PRE_CANARY_BLOCKED` until external evidence and
an enabled external live capability exist.

Required ordered stages: authenticated read-only; balance/allowance dry run;
post-only place/cancel; resting maker probe; optional controlled FAK probe;
partial-fill/cancel handling; settlement lifecycle; reconciliation; attestation.
Each stage stops on unknown order, heartbeat,
private-stream, fee, signer, limit, reconciliation, restriction or drift fault.
