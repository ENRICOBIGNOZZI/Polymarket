# V7 singleton reconciliation

The V7 consolidation does not replace the canonical runtime ownership boundary.

- `scripts/paper_latest_loop.sh` remains the outer runtime selector and singleton owner.
- `scripts/runtime_singleton_launcher.py` remains the lock owner.
- `ops/update_server_macos.sh` remains the explicit handoff implementation.
- V7 is selected as the champion plane only after integration/promotion; it must not create a second singleton authority.
- Fast Arb remains a shadow plane supervised by the canonical selector.

The consolidated V7 tests must extend, not rewrite, these incumbent ownership invariants.
