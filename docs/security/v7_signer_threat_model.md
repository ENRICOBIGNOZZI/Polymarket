# V7 signer threat model

The signer is a separately operated private control-plane service, not a
strategy process. It may only authorize pre-approved CLOB order payloads and
cannot sign arbitrary transactions.

Threats: key theft, compromised strategy/host/CI, malformed or malicious intent,
replay, wrong chain/contract/token, stale envelope, size escalation, session-key
overreach, clock fault, and emergency-revoke failure.

Required controls: hardware/KMS-backed key where available; allowlisted chain,
contracts, markets and tokens; price/size/type/post-only/expiry limits; monotonic
intent IDs; policy/SHA/envelope checks; independent kill/revoke; append-only
redacted signing audit; no key in source, CLI, logs or general process env.

This repository supplies schemas and gates only. Deployment, keys, approvals and
revocation evidence are private external requirements.

[`scripts/v7_signer_gateway.py`](../../scripts/v7_signer_gateway.py) implements
the public deterministic admission boundary. It always returns `DENY` and has
no private-key or transport capability, even for a structurally valid envelope.
