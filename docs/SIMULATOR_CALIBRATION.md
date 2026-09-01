# V7 simulator calibration support

The PAPER simulator is a research instrument. It may be used for capacity or
policy search only when `v7_simulator_calibration_support.py` finds an exact,
mature calibration cell from immutable `LIVE_OBSERVED` probe evidence. A PAPER
probe is diagnostic only and never supplies this support.

The decision is keyed by queue, spread, time-to-expiry, volatility, activity
and quote-lifetime buckets. Any missing cell, incomplete outcome tape, invalid
row, non-live calibration or mismatched model SHA is fail-closed. The result
always sets `live_execution_authorized=false`.

```text
python3 scripts/v7_simulator_calibration_support.py \
  --calibration immutable-calibration.json \
  --context-json '{"queue_bucket":"...","spread_bucket":"...","tte_bucket":"...","volatility_bucket":"...","activity_bucket":"...","quote_lifetime_bucket":"..."}'
```
