# V7 experiment scheduler

`scripts/v7_experiment_scheduler.py` records immutable, terminal receipts for
pre-registered research attempts. It is an offline admission and lineage tool:
it does not run a supplied command, train a model, use a credential, or alter
execution authority.

Each receipt binds the registered experiment hash, code SHA, data-manifest
hash and random seed. It records measured wall time, CPU/GPU/memory use, cached
intermediate hashes, output hashes, stopping condition and any failure reason.
Measured resources over the pre-registered budget are rejected.

A failed or stopped attempt may resume only from the immediately preceding
receipt and with the same immutable experiment identity. A completed attempt
cannot resume, preventing final-holdout reuse under the same experiment.

```text
python3 scripts/v7_experiment_scheduler.py validate --experiment experiment.json --run run.json
python3 scripts/v7_experiment_scheduler.py record --root /durable/experiments \
  --experiment experiment.json --run run.json
```
