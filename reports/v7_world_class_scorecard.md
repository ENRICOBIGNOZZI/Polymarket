# V7 world-class scorecard

Status: `MORE_EVIDENCE_REQUIRED`. The scorecard deliberately records no numeric
economic, latency, capacity or reliability result until it is reconstructed from
immutable real evidence. It does not infer results from PAPER activity.

`REAL_PNL_VERIFIED = FALSE` and `WORLD_CLASS_CANDIDATE = FALSE`.

Generate the runtime artifact from the exact checkout with:

```bash
python3 scripts/v7_world_class_scorecard.py current . \
  --output artifacts/v7_world_class/scorecard.json
```

The command never converts PAPER, simulated, or missing evidence into a
world-class claim.
