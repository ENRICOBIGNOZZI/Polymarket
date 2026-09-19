from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_probability_activation_is_explicit_sha_gated_and_restart_persistent():
    cutover=(ROOT/"ops/v7_london_cutover.sh").read_text()
    unit=(ROOT/"ops/systemd/polymarket-v7-paper.service.in").read_text()
    assert "POLYMARKET_PROBABILITY_MODEL_SOURCE" in cutover
    assert "v.get('code_sha')==sha" in cutover
    assert "int(v.get('test_duration_seconds') or 0)==7200" in cutover
    assert "not (v.get('excluded_assets') or [])" in cutover
    assert "PM_V7_PROBABILITY_EVALUATION_SECONDS=7200" in cutover
    assert "EnvironmentFile=-@RUN_ROOT@/control/probability-model.env" in unit
    assert 'rm -f "$PROBABILITY_ENV"' in cutover

def test_default_cutover_does_not_activate_probability_model():
    cutover=(ROOT/"ops/v7_london_cutover.sh").read_text()
    assert "POLYMARKET_PROBABILITY_MODEL_SOURCE:-" in cutover
    assert 'if [[ -n "$PROBABILITY_MODEL_SOURCE" ]]; then' in cutover
