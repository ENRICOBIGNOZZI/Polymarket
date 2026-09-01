# V7 deterministic replay parity

`scripts/v7_replay_parity.py` is an offline, fail-closed comparator for a
captured interval and its deterministic replay. Both manifests bind an exact
code SHA, immutable raw-data manifest hash, causal receive-time cut, canonical
reason codes and a hash of each stage payload.

It covers the required decision path: universe, validated book, feature
snapshot, strategy intent, risk decision and simulated OMS decision. A changed
wall-observation timestamp alone is classified as `EXPECTED_NONDETERMINISM`.
Missing input, causal-clock drift and payload/reason-code changes block release.

```text
python3 scripts/v7_replay_parity.py validate captured.json
python3 scripts/v7_replay_parity.py compare --captured captured.json --replay replay.json \
  --output artifacts/by_sha/<sha>/<run-id>/replay-parity.json
```

The output path is immutable: rerunning with identical bytes is accepted, while
any collision or symlink fails closed. The utility does not collect data, access
private state, sign, or submit orders.
