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

def test_cutover_validates_previous_systemd_run_root_before_new_root():
    s=(ROOT/'ops/v7_london_cutover.sh').read_text()
    capture=s.index('PREVIOUS_RUN_ROOT="$(systemctl show polymarket-v7-paper.service')
    stop=s.index('systemctl stop polymarket-v7-paper.service')
    guard=s.index('"$PREVIOUS_RUN_ROOT" != "$RUN_ROOT"', stop)
    previous_prepare=s.index('--run-root "$PREVIOUS_RUN_ROOT"', guard)
    target_prepare=s.index('--run-root "$RUN_ROOT"', previous_prepare)
    switch=s.index('ln -sfn "by-sha/$EXPECTED_SHA"')
    assert capture < stop < guard < previous_prepare < target_prepare < switch
    assert 'previous London run root is not absolute' in s

def test_cutover_assigns_run_root_to_service_user_before_runtime_start():
    s=(ROOT/'ops/v7_london_cutover.sh').read_text()
    prepare=s.index('v7_prepare_cutover_run_root.py')
    ownership=s.index('install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$RUN_ROOT" "$RUN_ROOT/control"')
    start=s.index('systemctl enable --now polymarket-v7-paper.service')
    assert prepare < ownership < start
    assert 'SERVICE_GROUP="$(id -gn "$SERVICE_USER")"' in s

def test_cutover_separates_core_runtime_health_from_full_data_readiness():
    s=(ROOT/'ops/v7_london_cutover.sh').read_text()
    core=s.index("'polymarket_v7_execution_alive 1'")
    safe=s.index("'polymarket_v7_economic_new_risk_ready 0'")
    full=s.index("exporter_full_health=")
    assert core < safe < full
    assert "London PAPER core runtime health gate failed" in s
    assert "exporter_full_health=%s" in s
    assert "'polymarket_v7_health 1'" not in s[core:full]


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


def test_native_carryover_is_explicit_and_requires_stable_run_root():
    s=(ROOT/'ops/v7_london_cutover.sh').read_text()
    assert 'PM_V7_ALLOW_NATIVE_CARRYOVER' in s
    assert 'native carryover requires a stable London run root' in s
    assert 'prepare_args+=(--allow-native-carryover)' in s
    guard=s.index('native carryover requires a stable London run root')
    stop=s.index('systemctl stop polymarket-v7-paper.service')
    assert guard < stop


def test_cutover_parses_systemd_environment_with_shlex_not_broken_tr_escape():
    s=(ROOT/'ops/v7_london_cutover.sh').read_text()
    assert 'python3 -c \'import shlex,sys;' in s
    assert 'x.startswith("PM_V7_RUN_ROOT=")' in s
    assert "tr ' ' '\\\\n'" not in s


def test_linux_runtime_pins_artifacts_to_exact_sha_generation():
    unit=(ROOT/'ops/systemd/polymarket-v7-paper.service.in').read_text()
    cutover=(ROOT/'ops/v7_london_cutover.sh').read_text()
    deploy=(ROOT/'.github/workflows/v7-deploy-paper-server.yml').read_text()

    exact='Environment=PM_V7_RUNTIME_ARTIFACT_ROOT=/home/@SERVICE_USER@/polymarket-artifacts/by-sha/@EXPECTED_SHA@'
    assert exact in unit
    assert 'PM_V7_RUNTIME_ARTIFACT_ROOT=/home/@SERVICE_USER@/polymarket-artifacts/current' not in unit
    assert 'TARGET_ARTIFACT="$ARTIFACT_ROOT/by-sha/$EXPECTED_SHA"' in cutover
    assert 'python3 - "$TARGET_ARTIFACT/manifest.json" "$EXPECTED_SHA"' in cutover

    stop=cutover.index('systemctl stop polymarket-v7-paper.service')
    start=cutover.index('systemctl enable --now polymarket-v7-paper.service')
    scrape=cutover.index('Prometheus is not scraping the V7 exporter')
    advance=cutover.index('ln -sfn "by-sha/$EXPECTED_SHA" "$ARTIFACT_CURRENT"')
    assert stop < start < scrape < advance

    # The deploy workflow may stage by-sha artifacts, but must not move the
    # shared convenience pointer before the exact-SHA cutover has succeeded.
    assert "ln -sfn 'by-sha/$deploy_sha' \\$HOME/polymarket-artifacts/current" not in deploy
