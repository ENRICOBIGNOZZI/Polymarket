from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts" / "paper_v7_execution_loop.sh").read_text(encoding="utf-8")

# macOS ships Bash 3.2. Under `set -u`, expanding an empty optional array can
# abort the canonical PAPER supervisor. Keep Graph/RV optional arguments on the
# zero-argument-safe positional-parameter path instead.
assert "joint_args=()" not in SCRIPT
assert '${joint_args[@]}' not in SCRIPT
assert "run_graph_rv()" in SCRIPT
assert 'run_graph_rv --joint-model "$RUN_ROOT/learned_execution/joint_policy.json"' in SCRIPT
assert "else\n      run_graph_rv\n    fi" in SCRIPT
