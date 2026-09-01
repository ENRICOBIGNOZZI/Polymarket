from __future__ import annotations

import inspect
import math
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable


_ASSERTIONS = unittest.TestCase()
_MISSING = object()


class _MonkeyPatch:
    """Small, deterministic subset of pytest's monkeypatch fixture for tests."""

    def __init__(self) -> None:
        self._undo: list[tuple[object, str, object]] = []

    def setattr(self, target: object, name: str, value: object) -> None:
        self._undo.append((target, name, getattr(target, name, _MISSING)))
        setattr(target, name, value)

    def undo(self) -> None:
        for target, name, previous in reversed(self._undo):
            if previous is _MISSING:
                delattr(target, name)
            else:
                setattr(target, name, previous)
        self._undo.clear()


def raises(error: type[BaseException], match: str):
    return _ASSERTIONS.assertRaisesRegex(error, match)


def approximately(actual: float, expected: float, *, tolerance: float = 1e-12) -> bool:
    return math.isclose(actual, expected, rel_tol=tolerance, abs_tol=tolerance)


def function_test_loader(namespace: dict[str, Any]) -> Callable[..., unittest.TestSuite]:
    """Adapt a constrained pytest-style fixture subset to stdlib unittest."""

    def load_tests(_loader: Any, _tests: Any, _pattern: Any) -> unittest.TestSuite:
        suite = unittest.TestSuite()
        for name, function in sorted(namespace.items()):
            if not name.startswith("test_") or not inspect.isfunction(function):
                continue
            parameters = tuple(inspect.signature(function).parameters)
            if not parameters:
                call = function
            elif parameters == ("tmp_path",):
                def call(function: Callable[..., Any] = function) -> None:
                    with tempfile.TemporaryDirectory(prefix="v7-test-") as directory:
                        function(Path(directory))
            elif parameters == ("tmp_path", "monkeypatch"):
                def call(function: Callable[..., Any] = function) -> None:
                    with tempfile.TemporaryDirectory(prefix="v7-test-") as directory:
                        patch = _MonkeyPatch()
                        try:
                            function(Path(directory), patch)
                        finally:
                            patch.undo()
            else:
                raise TypeError(f"unsupported function-test parameters for {name}: {parameters}")
            suite.addTest(unittest.FunctionTestCase(call, description=name))
        return suite

    return load_tests
