# Economic recovery research — 18 September 2026

This package is **PAPER research only**. It has no exchange order API, no wallet,
and no authority to deploy, increase risk, or promote a model.

## What exists

`causal_replay.py` implements arrival-time local-book simulation, visible depth
consumption, inventory-backed selling, fees, capped capital and conservative
maker print allocation. A missing arrival book or a capture gap is censored,
not a zero return. It is not an exchange FIFO reconstruction.

`evidence.py` freezes ordinary closed files into a new directory, checks hashes,
verifies a manifest and restore-tests the copy. It never deletes originals.

`inference.py` provides paired moving-block inference, chronological splits and
label-overlap purging. Asset observations stay inside common time blocks.
Bonferroni-adjusted intervals are available for preregistered multiple tests.

`models.py` fits a small regularized Bernoulli residual to the Polymarket prior.
Labels must have been observed before the training cutoff. A fitted artifact is
hashed and still explicitly unvalidated: fitting is not evidence of profit.

`risk_capacity.py` is a test/reference account for retained unsettled capital.
The production manager uses `scripts/v7_native_risk_policy.py`, not this reference
account, and there remains exactly one native execution owner.

`run.py` verifies an immutable dataset and generates a reproducible diagnosis.
With closed native observations it also runs arrival/size execution diagnostics.
The report does not claim T1–T8 are empirically completed just because their
protocol or simulator exists. The original frozen ledger has no reconstructable
native consumed-event tape; no replacement data is invented.

## Run

```sh
python3 research/economic/run.py --dataset /path/to/verified-dataset --output /new/path/report.json
python3 research/economic/evidence.py verify /path/to/verified-dataset
python3 research/economic/evidence.py restore /path/to/verified-dataset
python3 -m pytest -q tests/test_v7_economic_replay.py tests/test_v7_native_settlement_projection.py
```

Every output path must be new. Keep protocol, dataset, code and model hashes
separate. Do not search the held-out window for thresholds.

## Native capture

The C++ evidence writer captures the book/event/decision state actually consumed
by the owner. Books contain up to ten price levels. Explicit event clocks are
separate from the last book-update clock. A capture is usable as closed evidence
only with its healthy `.jsonl.closed.json` marker, matching sequence count and a
verified frozen copy. Model and policy hashes refer to parsed content bytes.

In the integrated 30-context manager, full native capture is explicitly opt-in
(`--capture-native-observations`). Keep it off in unbounded production until
storage and verified offload cover the expected volume. `--observation-only`
turns capture on automatically in a bounded isolated probe and makes the engine research-only: candidate
observations are recorded, but admission and all PAPER economic orders are
skipped. Use a new isolated root; never point a probe at the running ledger.
`--maker-share-cap-microunits 5000000` is a candidate parameter, not an automatic
change to the baseline or to the user's risk appetite. Baseline remains one.

## Rollout conditions

The manager's `--asynchronous-settlement` remains opt-in. Unresolved fills are
retained as capital claims, including fees. A validated committed FINAL is the
only release of a closed market's claim. A canonical-commit barrier prevents a
spool publication race from making filled exposure vanish at rollover.
The baseline still uses synchronous settlement and does not increase quote size.
The integration retains main's six-asset, five-horizon partitioned manager.
Each context keeps a fixed slice of the canonical allowance; unresolved claims
are deducted from that slice before a new window starts. Persistent feed
connections across windows remain separate work.

Before production cutover: exact-SHA CI; validate the integrated all-crypto
revision without dropping its changes; isolated public-feed capture; runtime
resource/overflow test; all unresolved positions reconciled or imported through
the canonical boundary; fresh run identity and unchanged approved risk caps.

## Economic acceptance

Five resolved markets do not establish profit. The first frozen baseline report
is INCONCLUSIVE. Outcomes, arrival parity, full capture coverage, held-out
comparisons, fixed infrastructure costs and an adequate number of time blocks
remain required. Zero-recovery risk equity is not marked-to-market PnL.
