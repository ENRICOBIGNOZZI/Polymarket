import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "v7_linux_hft_prepare", ROOT / "ops/v7_linux_hft_prepare.py")
PREP = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(PREP)


def test_cpu_list_compression():
    assert PREP.cpu_list([4, 5, 6, 7]) == "4-7"
    assert PREP.cpu_list([1, 3, 4, 7]) == "1,3-4,7"


def test_grub_contains_only_baseline_not_nic_tuning():
    text = PREP.render_grub([4, 5, 6, 7])
    assert "isolcpus=domain,managed_irq,4-7" in text
    assert "nohz_full=4-7" in text
    assert "rcu_nocbs=4-7" in text
    assert "transparent_hugepage=never" in text
    assert "busy_poll" not in text
    assert "cstate" not in text.lower()


def test_sysctl_is_low_jitter_baseline():
    text = PREP.render_sysctl()
    assert "vm.swappiness=1" in text
    assert "kernel.numa_balancing=0" in text
    assert "kernel.nmi_watchdog=0" in text
    assert "net.core.busy_poll" not in text


def test_validate_accepts_exact_eight_core_plan():
    policy = json.loads((ROOT / "config/v7_linux_hft_host_policy.json").read_text())
    plan = {
        "schema": "polymarket_v7_runtime_resource_plan_v2",
        "outer_cpuset_contract_satisfied": True,
        "cpu_classes_disjoint": True,
        "hot_path_cpus": [4, 5, 6, 7],
    }
    assert PREP.validate(policy, plan) == [4, 5, 6, 7]
