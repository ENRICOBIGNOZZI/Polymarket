import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "v7_runtime_resource_plan", ROOT / "scripts/v7_runtime_resource_plan.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def config():
    return json.loads((ROOT / "config/v7_runtime_resources.json").read_text())


def test_eight_cpu_layout_is_disjoint_and_exact():
    plan = MODULE.resolve(config(), list(range(8)))
    assert plan["outer_cpuset_contract_satisfied"] is True
    assert plan["cpu_classes_disjoint"] is True
    assert plan["housekeeping_cpus"] == [0]
    assert plan["control_cpus"] == [1]
    assert plan["collector_cpus"] == [2, 3]
    assert plan["hot_path_cpus"] == [4, 5, 6, 7]
    assert plan["spare_cpus"] == []


def test_outer_cpuset_below_eight_fails_closed():
    plan = MODULE.resolve(config(), list(range(7)))
    assert plan["outer_cpuset_contract_satisfied"] is False
    assert plan["hot_path_cpus"] == []


def test_larger_host_keeps_four_high_cpus_hot():
    plan = MODULE.resolve(config(), list(range(12)))
    assert plan["hot_path_cpus"] == [8, 9, 10, 11]
    assert plan["housekeeping_cpus"] == [0]
    assert plan["control_cpus"] == [1]
    assert plan["collector_cpus"] == [2, 3]
    assert plan["spare_cpus"] == [4, 5, 6, 7]


def test_cpu_list_parser_handles_ranges_and_sparse_sets():
    assert MODULE._parse_cpu_list("0-3,5,7-8\n") == [0, 1, 2, 3, 5, 7, 8]
    assert MODULE._parse_cpu_list("") == []


def test_explicit_outer_cpuset_still_fails_closed_even_with_isolation_logic():
    plan = MODULE.resolve(config(), [0, 1, 2, 3])
    assert plan["outer_cpuset_contract_satisfied"] is False
    assert plan["hot_path_cpus"] == []
