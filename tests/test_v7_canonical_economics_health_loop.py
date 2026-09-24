#!/usr/bin/env python3
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    launcher = (ROOT / "scripts" / "paper_v7_execution_loop.sh").read_text()
    call = 'scripts/v7_canonical_economics.py'
    assert launcher.count(call) == 1
    canonical = launcher.index(call)
    window = launcher[canonical:]
    assert 'sleep 60' in window
    assert ') & v7_register_child "$!"' in window
    assert 'lightweight operational reconciliation' in launcher
    sys.path.insert(0, str(ROOT / 'scripts'))
    from v7_process_manifest import resolve
    manifest = json.loads((ROOT / 'config/v7_process_manifest.json').read_text())
    resolved = resolve(ROOT, manifest)
    expected = resolved['expected_launcher_child_count']
    assert resolved['launcher_child_count'] == expected
    assert resolved['launcher_manifest_parity'] is True
    assert f'v7_assert_registered_child_count {expected}' in launcher
    research=(ROOT/'research/run_offline_analytics.sh').read_text()
    assert 'v7_generate_economic_artifacts.py' in research
    assert 'v7_profit_attribution.py' in research


if __name__ == "__main__":
    main()
