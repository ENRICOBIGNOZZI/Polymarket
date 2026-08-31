# V7 safe shutdown

Stop new risk, enter `DRAIN_ONLY`, preserve cancellation capacity, cancel orders,
persist OMS/journal checkpoints, reconcile private state, record health and stop
the sole writer. Restart begins read-only; it never resumes execution blindly.
