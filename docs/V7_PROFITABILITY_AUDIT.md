# V7 profitability audit

Profitability claims must use deduplicated canonical ledger evidence. Old C++
runtimes restarted their `record_id` counter at SHA cutovers, so the durable
identity is `(model_sha, record_id)`, not `record_id` alone. The audit reports
those legacy collisions explicitly and also handles overlapping retention data.

Run the audit against both durable archives and the current run:

```bash
python3 scripts/v7_profitability_audit.py \
  --input runs/paper_v7_archives \
  --input runs/paper_v7_live \
  --output artifacts/v7_profitability_audit.json
```

The report includes malformed/conflicting-record checks, unique orders and
fills, canonical terminal PnL, maker markouts, External Fair forecast scores,
and a market-implied benchmark. It exits with status 2 when evidence integrity
fails closed.

The External Fair taker has zero PAPER execution authority while its frozen
model remains economically immature. It runs continuously as a shadow collector
and writes `external_fair/counterfactuals.jsonl`; that tape does not enter the
execution ledger or portfolio cash. It records trade-independent forecasts at
canonical 240/180/120/90/60/45/30/20/15/10/5-second TTE buckets, virtual fills
only after fresh-book revalidation, and forecast scores after settlement.
Promotion requires a separate reviewed
configuration change after forward out-of-sample calibration and net-PnL gates
beat the market benchmark. Real-money execution remains out of scope.
