# V7 signer threat model

The signer is a separately operated private control-plane service, not a
strategy process. The checked-in system cannot authorize, sign, or submit CLOB
order payloads, and no implementation may sign arbitrary transactions.

Threats: key theft, compromised strategy/host/CI, malformed or malicious intent,
replay, wrong chain/contract/token, stale control state, size escalation, session-key
overreach, clock fault, and emergency-revoke failure.

Required controls: hardware/KMS-backed key where available; allowlisted chain,
contracts, markets and tokens; price/size/type/post-only/expiry limits; monotonic
intent IDs; policy/SHA checks; independent kill/revoke; append-only
redacted signing audit; no key in source, CLI, logs or general process env.

This repository supplies schemas and gates only. Deployment, keys, rotation and
revocation evidence are private external requirements.

[`scripts/v7_signer_gateway.py`](../../scripts/v7_signer_gateway.py) implements
the public deterministic admission boundary. It always returns `DENY` and has
no private-key or transport capability, even for a structurally valid live intent.
