# Research plane

This directory owns training, retrospective analysis, benchmark generation and artifact promotion.
It consumes copied/synchronized crypto evidence. It has no OMS, capital, ledger-writer or execution authority.
London consumes only immutable artifacts produced here and never trains models.

The [cumulative learning pipeline](learning/README.md) owns the daily midnight
Europe/Zurich job. It compares three settlement forecasters (PM, logistic offset,
gradient boosting), records immutable private receipts and never auto-promotes.
The manual legacy artifact utilities remain available for historical workflows;
they are no longer invoked by the daily scheduler.
