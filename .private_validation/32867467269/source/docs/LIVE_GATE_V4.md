# V4 live gate

The real-money pilot is not a default mode. Before `scripts/tiny_live_pilot.py --execute` is allowed, the paper ledger must produce a walk-forward report with `eligible_for_tiny_pilot=true`. The pilot remains capped at one bundle, $10 total and $5 per leg, and requires an explicit environment consent string plus credentials.

Grafana/Prometheus are operational gates, not decoration: a stale trade recorder, stale multi-leg broker state, an active kill switch, or severe partial-fill imbalance must block escalation to real money.
