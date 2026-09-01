# V7 platform-contract evidence

`config/v7_platform_contract.json` is a strict, checked-in baseline, not an
authorization to trade. A fresh public-document review must be captured before
any future mode can rely on it. The capture contains every URL in
`official_sources`, the exact snapshot used for comparison, and the verifier's
drift result.

The current baseline deliberately pins the V2 CTF collateral adapters, not the
deprecated CLOB V1 negative-risk adapter. That distinction is checked against
the V2/pUSD registry and fails closed on drift.

Per-market facts are deliberately not frozen as universal constants. The
registry requires archived values and hashes for exchange selection, negative
risk, tick size, minimum size, fees, rewards, taker delay, settlement rules,
WebSocket schema, and rate-limit tier. A missing value is drift, not zero or a
safe default.

The Data API activity boundary separately pins its documented offset pagination,
maximum page size, and ascending timestamp windows. It is verified from raw
pages before it can contribute to reconciliation.

The archiver is deliberately offline. Obtain official public document bytes by
an approved read-only process, then archive them without embedding credentials:

```text
python3 scripts/v7_platform_contract_archive.py archive \
  --root . --registry config/v7_platform_contract.json \
  --snapshot /secure/read-only/platform-snapshot.json \
  --exact-code-sha <40-lowercase-hex-sha> --run-id <immutable-run-id> \
  --source-document 'https://docs.polymarket.com/...=/secure/read-only/source.html' ...
```

The command stores content-addressed, immutable pointers below
`artifacts/by_sha/<sha>/<run-id>/`; it does not fetch URLs, sign, submit, or
cancel orders. Recheck the returned archive pointer and its SHA-256 with
`verify --manifest <artifact path> --archive-sha256 <returned sha256>`. A changed endpoint, contract, protocol
field, or stale snapshot is reported as drift and keeps the required mode
read-only.
