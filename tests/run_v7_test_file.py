#!/usr/bin/env python3
"""Run function tests through pytest; retain explicit script-only contracts."""
from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def _test_environment():
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(ROOT) + (os.pathsep + existing if existing else "")
    environment.pop("PYTHONOPTIMIZE", None)
    return environment


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: run_v7_test_file.py TEST_FILE", file=sys.stderr)
        return 64
    path = Path(sys.argv[1]).resolve()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions = [node for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name.startswith("test_")]
    if functions:
        # No ambient plugins or optimized-away Python assertions in CI.
        environment = _test_environment()
        environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        environment.pop("PYTEST_ADDOPTS", None)
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", str(path)],
            env=environment, cwd=ROOT, check=False,
        ).returncode
    has_entrypoint = any(isinstance(node, ast.If)
                         and "__name__" in ast.unparse(node.test)
                         and "__main__" in ast.unparse(node.test)
                         for node in tree.body)
    has_assertions = any(isinstance(node, ast.Assert) for node in tree.body)
    if not has_entrypoint and not has_assertions:
        print(f"NO_EXECUTABLE_TESTS: {path}", file=sys.stderr)
        return 5
    environment = _test_environment()
    return subprocess.run([sys.executable, str(path)], env=environment,
                          cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
