# Security policy

## Supported version

Security fixes target the current `main` revision of V7. Historical revisions
and retired runtime generations are not supported.

## Reporting a vulnerability

Do not open a public issue containing exploit details, credentials, private
infrastructure identifiers, or sensitive market/account data. Use the
repository's private GitHub security-advisory channel to report the affected
revision, impact, reproduction steps, and a minimal proof of concept. Please
allow the maintainer time to confirm and remediate the issue before disclosure.

## Safety boundary

The canonical repository is PAPER-only:

- `paper_only = true`
- `authenticated_execution = false`
- `real_order_submission = false`
- `real_capital_at_risk = false`

A change that introduces credentials, wallet signing, authenticated execution,
or real order submission violates the supported security boundary and must not
be merged.

## Secrets and build integrity

Never commit API tokens, private keys, seed phrases, cookies, `.env` files, or
production host material. Use the platform's encrypted secret store for CI and
runtime configuration. `./scripts/verify_v7.sh` performs a high-confidence
tracked-secret scan and produces a hashed build manifest with a dependency
inventory. Canonical economic runs must reference immutable dataset, universe,
model, configuration, binary, and build-manifest identities.
