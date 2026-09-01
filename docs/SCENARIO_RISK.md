# V7 scenario risk

`scripts/v7_scenario_risk.py` is a deterministic, offline worst-case calculator
for active resting, delayed, unsettled and settlement-pending binary positions.
Inputs use signed integer base-unit PnL for each resolution. It enumerates only
feasible outcomes after mutually-exclusive and implication constraints, then
applies delayed-settlement, market/oracle/feed, venue, unwind, reward, fee and
network-partition stresses.

The report aggregates worst cases by event, category and oracle. Missing stress
dimensions, an unknown constraint condition, invalid status or too many
conditions for exact enumeration fail closed. The calculator always reports
`live_execution_authorized=false`.

```text
python3 scripts/v7_scenario_risk.py --book risk-book.json
```
