import importlib.util
import json
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "v7_linux_hft_host_audit", ROOT / "ops/v7_linux_hft_host_audit.py")
AUDIT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(AUDIT)
POLICY = json.loads((ROOT / "config/v7_linux_hft_host_policy.json").read_text())
PLAN = {"hot_path_cpus": [4, 5, 6, 7]}


def put(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)


def build_clean(root: Path) -> None:
    put(root, "sys/devices/system/cpu/online", "0-7\n")
    put(root, "sys/devices/system/cpu/smt/active", "0\n")
    for cpu in range(8):
        base = f"sys/devices/system/cpu/cpu{cpu}/topology"
        put(root, f"{base}/physical_package_id", "0\n")
        put(root, f"{base}/core_id", f"{cpu}\n")
        put(root, f"sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_governor", "performance\n")
    put(root, "sys/devices/system/cpu/isolated", "4-7\n")
    put(root, "sys/devices/system/cpu/nohz_full", "4-7\n")
    put(root, "sys/kernel/mm/transparent_hugepage/enabled", "always madvise [never]\n")
    put(root, "proc/cmdline", "quiet isolcpus=domain,managed_irq,4-7 nohz_full=4-7 rcu_nocbs=4-7 transparent_hugepage=never\n")
    put(root, "proc/sys/vm/swappiness", "1\n")
    put(root, "proc/sys/kernel/numa_balancing", "0\n")
    put(root, "proc/sys/kernel/nmi_watchdog", "0\n")


def test_clean_eight_core_baseline_is_ready():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build_clean(root)
        result = AUDIT.audit(POLICY, root, PLAN)
        assert result["hard_failures"] == []
        assert result["baseline_failures"] == []
        assert result["baseline_ready"] is True
        assert result["hot_path_cpus"] == [4, 5, 6, 7]


def test_missing_hot_core_isolation_fails_baseline():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build_clean(root)
        put(root, "sys/devices/system/cpu/isolated", "4-6\n")
        result = AUDIT.audit(POLICY, root, PLAN)
        assert "hot_cpus_missing_from_isolcpus" in result["baseline_failures"]
        assert result["baseline_ready"] is False


def test_smt_and_small_outer_cpuset_fail_hard():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build_clean(root)
        put(root, "sys/devices/system/cpu/online", "0-5\n")
        put(root, "sys/devices/system/cpu/smt/active", "1\n")
        for cpu in range(6):
            put(root, f"sys/devices/system/cpu/cpu{cpu}/topology/core_id", f"{cpu // 2}\n")
        result = AUDIT.audit(POLICY, root, PLAN)
        assert "outer_cpuset_below_eight_cpus" in result["hard_failures"]
        assert "insufficient_physical_cores" in result["hard_failures"]
        assert "smt_or_sibling_contention" in result["hard_failures"]


def test_cpu_list_helpers():
    assert AUDIT.parse_cpu_list("0-2,4,6-7") == [0, 1, 2, 4, 6, 7]
    assert AUDIT.cmdline_cpu_arg("x=1 rcu_nocbs=4-7", "rcu_nocbs") == [4, 5, 6, 7]
    assert AUDIT.cmdline_cpu_arg("isolcpus=domain,managed_irq,4-7", "isolcpus") == [4, 5, 6, 7]
