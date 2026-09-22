from pathlib import Path
import os
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_ci_preflight_precedes_every_live_generation_mutation():
    source = (ROOT / "ops/v7_london_cutover.sh").read_text()
    gate = source.index('python3 "$TARGET_RUNTIME/scripts/v7_exact_sha_ci_gate.py"')
    for mutation in ['systemctl stop', 'v7_prepare_cutover_run_root.py',
                     'ln -sfn "by-sha/$EXPECTED_SHA"', 'systemctl enable --now']:
        assert gate < source.index(mutation)
    assert '--sha "$EXPECTED_SHA"' in source[gate:gate + 240]
    assert '--output "$CI_PREFLIGHT_RECEIPT"' in source[gate:gate + 240]
    runtime = (ROOT / 'scripts/paper_v7_execution_loop.sh').read_text()
    repository_line = next(x for x in runtime.splitlines() if x.startswith('CI_REPOSITORY='))
    assert repository_line in source


@pytest.mark.parametrize('ci_returncode', [0, 2, 3])
def test_real_shell_preflight_failure_cannot_reach_mutation(tmp_path, ci_returncode):
    source = (ROOT / "ops/v7_london_cutover.sh").read_text()
    # Exercise the production exact-SHA CI gate in isolation. Earlier model
    # and artifact gates have their own tests; they must not mask whether a
    # failed/pending CI gate can reach the first runtime mutation.
    ci_start = source.index('# Check the same exact-SHA CI contract')
    mutation = source.index('# Capture the run root currently bound to systemd')
    ci_section = source[ci_start:mutation]
    entry = tmp_path / 'preflight.sh'
    marker = tmp_path / 'mutation-reached'
    entry.write_text(
        'set -euo pipefail\n'
        'EXPECTED_SHA="${POLYMARKET_EXPECTED_SHA:?}"\n'
        'RUNTIME_ROOT="${POLYMARKET_RUNTIME_ROOT:?}"\n'
        'TARGET_RUNTIME="$RUNTIME_ROOT/by-sha/$EXPECTED_SHA"\n'
        + ci_section
        + '\nprintf reached > "$TEST_MUTATION_MARKER"\n'
    )
    sha = 'a' * 40
    runtime = tmp_path / 'runtime'
    (runtime / 'by-sha' / sha / 'deploy/london').mkdir(parents=True)
    (runtime / 'by-sha' / sha / 'deploy/london/runtime_sha').write_text(sha)
    binaries = tmp_path / 'bin'; binaries.mkdir()
    programs = {
        'uname': '#!/bin/sh\necho Linux\n',
        'id': '#!/bin/sh\necho 1000\n',
        'flock': '#!/bin/sh\nexit 0\n',
        'python3': ('#!/bin/sh\ncase "$1" in\n'
                    '*v7_exact_sha_ci_gate.py) exit "$TEST_CI_RC" ;;\n'
                    '-) cat >/dev/null; exit 0 ;;\n'
                    '*) exit 0 ;;\nesac\n'),
    }
    for name, contents in programs.items():
        path = binaries / name; path.write_text(contents); path.chmod(0o755)
    env = {**os.environ, 'PATH': str(binaries) + os.pathsep + os.environ['PATH'],
           'POLYMARKET_EXPECTED_SHA': sha, 'POLYMARKET_SERVICE_USER': 'fixture',
           'POLYMARKET_RUNTIME_ROOT': str(runtime),
           'POLYMARKET_LONDON_DEPLOY_LOCK_FILE': str(tmp_path / 'deploy.lock'),
           'TEST_CI_RC': str(ci_returncode), 'TEST_MUTATION_MARKER': str(marker)}
    result = subprocess.run(['bash', str(entry)], env=env, input='',
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == ci_returncode, result.stderr
    assert marker.exists() is (ci_returncode == 0)
