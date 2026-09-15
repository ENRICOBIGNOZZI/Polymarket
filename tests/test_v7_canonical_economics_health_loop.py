#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    launcher = (ROOT / "scripts" / "paper_v7_execution_loop.sh").read_text()
    call = 'python3 scripts/v7_canonical_economics.py --ledger "$RUN_ROOT/ledger/execution.jsonl"'
    assert launcher.count(call) == 1
    canonical = launcher.index(call)
    historical = launcher.index('last_historical_attribution_at=0')
    assert canonical < historical
    window = launcher[canonical:historical]
    assert 'sleep 60' in window
    assert ') & v7_register_child "$!"' in window
    assert 'Health-critical canonical economics has its own 60-second loop.' in launcher
    assert 'v7_assert_registered_child_count 25' in launcher


if __name__ == "__main__":
    main()
