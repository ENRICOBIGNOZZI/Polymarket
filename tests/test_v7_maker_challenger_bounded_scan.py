import inspect
from pathlib import Path

import ops.v7_maker_challenger_ssm as runner

ROOT=Path(__file__).resolve().parents[1]


def test_maker_challenger_archive_includes_causal_value_dependencies():
    required={
        "research/walk_forward_v2/__init__.py",
        "research/walk_forward_v2/core.py",
        "research/economic/causal_replay.py",
    }
    assert required.issubset(set(runner.SOURCE_PATHS))
    for relative in runner.SOURCE_PATHS:
        assert (ROOT/relative).is_file(), relative


def test_maker_horse_race_does_not_scan_entire_run_root():
    source=inspect.getsource(runner.execute)
    assert "--maker-evidence __RUN_ROOT__/ledger" in source
    assert "--maker-evidence __RUN_ROOT__/micro_maker" in source
    assert "--maker-evidence __RUN_ROOT__ \\" not in source
