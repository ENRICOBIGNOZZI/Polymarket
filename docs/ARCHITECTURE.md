# V7 Architecture

V7 is the only operational architecture. The canonical entrypoint is `scripts/paper_v7_execution_loop.sh` with `config/paper_v7.json`.

The runtime owns one execution authority, OMS path, physical inventory truth, capital allocator, portfolio risk guard and canonical ledger writer. Strategies emit candidate actions; they do not submit orders or mutate account state directly. The common action space is MAKE, TAKE, ARB, CANCEL, WITHDRAW and NOTHING.

High-frequency state changes are event-driven in C++. Python is restricted to control-plane work, research, reporting and model fitting. Every action is bound to receive-time causal state and exact code/model/policy identity.

Blue is the frozen live-PAPER incumbent and the only writer. Green may replay and shadow but has zero execution authority until an explicitly approved, exact-SHA cutover.
