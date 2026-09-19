# Native capture integrity follow-up

This extends PR #1197 on top of `e95d5ba15f0148d3dee31d313e1f72621d6a4a75`.
It changes the existing offline native repricing consumer, not the London
collector, model, order owner, runtime manifest or deployment workflow.

## Corrected defects

The previous join used only run, market and signal version. The native writer
already records capture, server and code identities. A restarted process can
reuse a signal version. Joins now retain all those identities; unidentified
legacy sources are isolated per input file rather than joined across files.

Partial JSONL tails, duplicate JSON keys, nonfinite JSON, conflicting rows,
duplicate source content and conflicting sealed-capture identities fail closed.
A sealed input requires the existing producer `.closed.json` sidecar: identity,
byte count, final sequence, watermark and health must match. Capture sequences
must be complete, and observation clocks cannot run backwards. Gzip decoding is
bounded and retains the original JSONL sidecar convention.

Labels are censored on mismatched decisions, reconnect epochs, context, tick,
market terms, recorded book faults or a nominal target at/after market close.
External features with future or missing receive clocks cannot enter a label.
A valid unchanged book still yields a legitimate zero midpoint delta. Missing
or broken coverage never becomes that zero.

Every output records source and row hashes. The CLI publishes new files only;
it refuses to overwrite existing evidence. Its default requires sealed inputs.
The existing Python `build(paths)` API remains diagnostic-compatible; unsealed
CLI inspection requires `--allow-unsealed-diagnostics` explicitly.

## What this does not establish

These are midpoint repricing diagnostics, not executable prices, actual fills,
portfolio PnL or permission to trade. Every row explicitly carries
`eligible_for_executable_training=false`. Native observation time is not silently
renamed to durable publication time. Producer closure verifies the captured file,
not lossless coverage of all exchange activity or availability of unseen orders.

The complete executable adapter still needs verified metadata, actual availability
clocks, depth coverage and price/quantity/fee semantics. No new probability model
has been fitted on live data, promoted or deployed by this change. The remaining
program is recorded in `config/v7_model_data_roadmap.json`; its pending work is
not cleared by this patch or by successful CI.

## Validation at development time

The focused local suite contains 90 passing tests and 11 passing subtests:
50 existing research-contract/dataset tests and 40 native-consumer cases.
Tests use deterministic synthetic fixtures, including the producer's real field
names and closure format. This is not a live-capture validation claim.

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q \
  tests/test_v7_native_repricing_dataset.py \
  tests/test_v7_research_economic_contract.py \
  tests/test_v7_research_causal_dataset.py
```

Full exact-head CI must be checked on the new commit. Earlier green CI belongs
to the earlier head. Desktop Commander reported exhausted monthly quota; no
retry or reconnection is authorized as a workaround. The current GitHub tools
have no workflow-dispatch action. London deployment and direct runtime/data
inspection therefore remain unperformed, not implicitly successful.
