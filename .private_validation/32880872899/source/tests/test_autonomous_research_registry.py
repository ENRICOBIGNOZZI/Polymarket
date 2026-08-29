#!/usr/bin/env python3
from __future__ import annotations

import copy
import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "validate_experiment_registry", SCRIPTS / "validate_experiment_registry.py"
)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


class AutonomousResearchRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.control = json.loads(
            (ROOT / "config" / "autonomous_research.json").read_text(encoding="utf-8")
        )
        self.registry = json.loads(
            (ROOT / "config" / "experiment_registry.json").read_text(encoding="utf-8")
        )

    def errors(self, control=None, registry=None):
        return validator.validate(
            ROOT,
            copy.deepcopy(self.control if control is None else control),
            copy.deepcopy(self.registry if registry is None else registry),
        )

    def test_repository_registry_is_fail_closed_and_valid(self) -> None:
        self.assertEqual(self.errors(), [])
        active = [
            item for item in self.registry["experiments"] if item.get("status") == "active"
        ]
        self.assertEqual(active, [])

    def test_active_missing_worker_is_rejected(self) -> None:
        control = copy.deepcopy(self.control)
        registry = copy.deepcopy(self.registry)
        item = registry["experiments"][0]
        item["status"] = "active"
        item["command"] = ["python3", "scripts/not_integrated_worker.py"]
        control["dispatcher"]["allowed_scripts"].append("scripts/not_integrated_worker.py")
        errors = validator.validate(ROOT, control, registry)
        self.assertTrue(any("active script is missing" in error for error in errors), errors)

    def test_active_unallowlisted_worker_is_rejected(self) -> None:
        registry = copy.deepcopy(self.registry)
        item = registry["experiments"][0]
        item["status"] = "active"
        item["command"] = ["python3", "scripts/validate_experiment_registry.py"]
        errors = validator.validate(ROOT, self.control, registry)
        self.assertTrue(any("script" in error and "not allowlisted" in error for error in errors), errors)

    def test_forbidden_execution_argument_is_rejected(self) -> None:
        control = copy.deepcopy(self.control)
        registry = copy.deepcopy(self.registry)
        item = registry["experiments"][0]
        item["status"] = "active"
        item["command"] = [
            "python3",
            "scripts/validate_experiment_registry.py",
            "--execute",
        ]
        control["dispatcher"]["allowed_scripts"].append(
            "scripts/validate_experiment_registry.py"
        )
        errors = validator.validate(ROOT, control, registry)
        self.assertTrue(any("forbidden argument token --execute" in error for error in errors), errors)

    def test_duplicate_experiment_id_is_rejected(self) -> None:
        registry = copy.deepcopy(self.registry)
        duplicate = copy.deepcopy(registry["experiments"][0])
        registry["experiments"].append(duplicate)
        errors = validator.validate(ROOT, self.control, registry)
        self.assertTrue(any("duplicate experiment_id" in error for error in errors), errors)

    def test_control_plane_cannot_enable_authenticated_execution(self) -> None:
        control = copy.deepcopy(self.control)
        control["allow_authenticated_execution"] = True
        errors = validator.validate(ROOT, control, self.registry)
        self.assertIn("authenticated execution must remain disabled", errors)


if __name__ == "__main__":
    unittest.main()
