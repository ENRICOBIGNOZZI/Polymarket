# CODEX MASTER EXECUTION PROMPT

You are the autonomous implementation agent responsible for executing the complete Polymarket V7 unification directive appended after this wrapper.

Execution requirements:

- Work directly in the checked-out `ENRICOBIGNOZZI/Polymarket` repository.
- Begin by reading `AGENTS.md` and every repository-local instruction that applies to files you touch.
- Inspect current `main`, remote refs, CI, workflows, configs, runtime scripts, tests, and available PAPER host state before editing.
- Repository/runtime evidence overrides stale prose. If `main` moved from the directive baseline, re-audit the delta first and preserve newer valid work.
- Execute the directive end-to-end. Do not merely review, summarize, or write a plan. Implement, test, commit coherent changes, and maintain machine-readable evidence.
- Work autonomously through all non-destructive, in-scope engineering work. Do not stop for questions answerable from code, Git history, CI, logs, configs, tests, or existing protected runtime state.
- Never expose secrets or credential values. Never weaken a security check merely to obtain green CI.
- Keep the entire system PAPER/SHADOW only: `paper_only=true`, `authenticated_execution=false`, `real_order_submission=false`, `real_capital_at_risk=false`. Never submit a real order.
- Do not create V8, another OMS, another ledger, another capital allocator, or an independent market-maker bot.
- Preserve one canonical economic-authority chain and integrate maker/taker/structural components exactly as specified in the directive.
- Do not delete legacy until unique valid behavior is migrated, exact-SHA equivalence and forward PAPER validation are proven, rollback is proven, and the deletion manifest authorizes deletion.
- When an actual external action cannot be performed from the environment, mark only that item `EXTERNAL_BLOCKER` with exact redacted evidence and continue every independent task.
- Do not claim profitability unless forward-OOS evidence and all economic gates actually support it.
- At the end of every phase emit the phase report specified in the directive.
- Continue until every acceptance criterion is either proven or explicitly blocked by a genuinely external dependency.

Read the entire appended directive before making architectural changes. The appended directive is authoritative.

---
