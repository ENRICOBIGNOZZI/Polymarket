# Development workflow

This repository uses `main` as the only authoritative code line. Code on any other branch is research-in-progress until it is merged.

## Branch roles

- `main`: stable, reproducible, paper-only engine. Every commit must build and pass deterministic tests.
- `feat/*`, `fix/*`, `improve/*`: short-lived implementation branches merged through a pull request.
- `experiment/*`, `diagnostic/*`: temporary live-data experiments. Their result must be summarized in the pull-request body or a durable document; the branch is then closed without merge unless it contains reusable, reviewed code.
- `ci/*`, `tmp/*`, `noop`: disposable infrastructure branches. They must not remain active after their purpose is complete.

At most one broad integration branch should be active at a time. Smaller independent changes should still use short-lived branches and narrowly scoped pull requests.

## Merge gates

A pull request is mergeable only when all applicable conditions hold:

1. Release and Debug builds complete successfully.
2. Deterministic unit and mock integration tests pass.
3. New behavior has tests or an explicit reason why a deterministic test is impossible.
4. Configuration, state persistence and failure behavior are documented.
5. Terminal-probability forecasts remain separate from mark-to-market relative-value signals.
6. Paper execution remains separate from authenticated real-money order submission.
7. No credentials, private keys, wallet secrets or generated runtime state are committed.
8. Externally dependent live-data checks are reported separately from required deterministic CI.

## Pull-request lifecycle

1. Create the branch from the current `main`.
2. Keep the pull request focused and document the economic interpretation of every signal and cost.
3. Use squash merge for normal feature work so `main` has one coherent commit per pull request.
4. Close experiments without merge after recording the result.
5. Delete the head branch after merge or closure.

A branch must never be kept alive merely to preserve history: Git commits, pull-request discussions and durable research notes are the historical record.

## Safety boundary

The repository is a research and paper-trading system. A future authenticated broker must be a separate adapter with its own reconciliation, balance checks, cancel/replace logic, real-fill confirmation and production kill switches. Compiling the forecasting engine is not sufficient authorization to trade real money.
