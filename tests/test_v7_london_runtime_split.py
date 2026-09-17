from __future__ import annotations
from pathlib import Path
import json, subprocess
ROOT=Path(__file__).resolve().parents[1]

def test_stage_does_not_switch_or_start_runtime():
    s=(ROOT/'ops/v7_london_stage_release.sh').read_text()
    assert 'PM_LONDON_RUNTIME_ONLY=ON' in s
    assert 'ctest --test-dir' in s
    assert 'build_london_runtime_bundle.py' in s
    assert 'ln -sfn' not in s
    assert 'systemctl enable' not in s
    assert 'systemctl stop' not in s

def test_cutover_artifact_gate_precedes_stop_and_symlink_switch():
    s=(ROOT/'ops/v7_london_cutover.sh').read_text()
    gate=s.index("runtime artifact bundle missing")
    stop=s.index('systemctl stop')
    switch=s.index('ln -sfn "by-sha/$EXPECTED_SHA"')
    assert gate < stop < switch
    assert "runtime_training') is False" in s
    assert "pgrep -af" in s

def test_runtime_bundle_is_research_free_by_contract():
    m=json.loads((ROOT/'deploy/london/runtime_manifest.json').read_text())
    assert m['paper_only'] is True and m['authenticated_execution'] is False and m['real_order_submission'] is False
    assert 'tests/' in m['forbidden_path_fragments']
    assert '/research/' in m['forbidden_path_fragments']
    assert all('train.py' not in x and 'durable_learning' not in x for x in m['python_entrypoints']+m['support_files'])

def test_research_cycle_has_no_execution_authority_and_hourly_template():
    cycle=(ROOT/'research/run_research_cycle.sh').read_text()
    plist=(ROOT/'ops/launchd/com.polymarket.v7.research-cycle.plist.in').read_text()
    assert 'pull_london_evidence.sh' in cycle
    assert 'build_runtime_artifacts.sh' in cycle
    assert 'push_runtime_artifacts.sh' in cycle
    assert "'execution_authority':False" in cycle
    assert '<integer>3600</integer>' in plist

def test_shells_parse():
    for rel in ['ops/v7_london_stage_release.sh','ops/v7_london_bootstrap.sh','ops/v7_london_cutover.sh','research/run_research_cycle.sh','research/install_macos_scheduler.sh']:
        r=subprocess.run(['bash','-n',str(ROOT/rel)],capture_output=True,text=True)
        assert r.returncode==0, r.stderr
