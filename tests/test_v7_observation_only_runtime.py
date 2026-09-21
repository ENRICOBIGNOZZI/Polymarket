from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_canonical_paper_loop_parks_native_model_in_observation_only():
    script = (ROOT / "scripts/paper_v7_execution_loop.sh").read_text()
    manager = script.split(
        "python3 scripts/v7_native_crypto_engine_manager.py", 1
    )[1].split(">> \"$RUN_ROOT/native_engine_manager.log\"", 1)[0]
    assert "--observation-only" in manager
    assert "--capture-native-decisions" in manager
    assert "--capture-execution-windows" in manager


def test_manager_propagates_observation_only_to_every_native_worker():
    source = (ROOT / "scripts/v7_native_crypto_engine_manager.py").read_text()
    assert 'parser.add_argument("--observation-only"' in source
    assert 'command.append("--observation-only")' in source
    assert '"observation_only": bool(getattr(self.args, "observation_only", False))' in source
    assert '"OBSERVATION_ONLY"' in source


def test_native_worker_blocks_before_authority_submit_in_observation_only():
    source = (ROOT / "src/v7_crypto_settlement_engine.cpp").read_text()
    observation = source.index("if (options.observation_only) continue;")
    authority = source.index("authority.submit(", observation)
    assert observation < authority
    # Research evidence is emitted before the fail-closed admission boundary.
    evidence = source.rfind("evidence_writer.healthy()", 0, observation)
    assert evidence >= 0
    assert evidence < observation


def test_observation_only_keeps_real_execution_impossible():
    service = (ROOT / "ops/systemd/polymarket-v7-paper.service.in").read_text()
    assert "PM_V7_AUTHENTICATED_EXECUTION=0" in service
    assert "PM_V7_REAL_ORDER_SUBMISSION=0" in service
