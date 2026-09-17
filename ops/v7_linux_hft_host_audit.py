#!/usr/bin/env python3
"""Read-only Linux HFT host audit. Never mutates kernel, NIC or IRQ state."""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

SCHEMA = "polymarket_v7_linux_hft_host_audit_v2"


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except (OSError, PermissionError):
        return None


def parse_cpu_list(raw: str | None) -> list[int]:
    if not raw:
        return []
    out: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "-" in item:
            lo, hi = (int(x) for x in item.split("-", 1))
            if hi < lo:
                raise ValueError("descending CPU range")
            out.update(range(lo, hi + 1))
        else:
            out.add(int(item))
    return sorted(out)


def cmdline_cpu_arg(cmdline: str, name: str) -> list[int]:
    match = re.search(r"(?:^|\s)" + re.escape(name) + r"=([^\s]+)", cmdline)
    if not match:
        return []
    raw = match.group(1)
    if name == "isolcpus":
        parts = raw.split(",")
        while parts and not (parts[0][0].isdigit() if parts[0] else False):
            parts.pop(0)
        raw = ",".join(parts)
    try:
        return parse_cpu_list(raw)
    except ValueError:
        return []


def allowed_cpus() -> list[int]:
    try:
        return sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return list(range(max(1, os.cpu_count() or 1)))


def cpu_topology(sys_root: Path, cpus: list[int]) -> dict[str, Any]:
    cores: dict[tuple[str, str], list[int]] = {}
    for cpu in cpus:
        topo = sys_root / "devices/system/cpu" / f"cpu{cpu}" / "topology"
        package = read_text(topo / "physical_package_id") or "0"
        core = read_text(topo / "core_id") or str(cpu)
        cores.setdefault((package, core), []).append(cpu)
    threads_per_core = max((len(v) for v in cores.values()), default=0)
    smt_raw = read_text(sys_root / "devices/system/cpu/smt/active")
    smt_active = smt_raw == "1" if smt_raw is not None else threads_per_core > 1
    return {
        "allowed_logical_cpus": cpus,
        "physical_cores": len(cores),
        "maximum_threads_per_core": threads_per_core,
        "smt_active": smt_active,
    }


def proc_running(proc_root: Path, name: str) -> bool:
    try:
        pids = [p for p in proc_root.iterdir() if p.name.isdigit()]
    except OSError:
        return False
    return any(read_text(pid / "comm") == name for pid in pids)


def run_readonly(command: list[str]) -> str | None:
    if not command or shutil.which(command[0]) is None:
        return None
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (result.stdout or result.stderr).strip()
    return text if text else None


def cpu_governors(sys_root: Path, cpus: list[int]) -> list[str]:
    values: set[str] = set()
    for cpu in cpus:
        value = read_text(sys_root / "devices/system/cpu" / f"cpu{cpu}" / "cpufreq/scaling_governor")
        if value:
            values.add(value)
    return sorted(values)


def cpu_idle_states(sys_root: Path, cpus: list[int]) -> list[dict[str, Any]]:
    if not cpus:
        return []
    base = sys_root / "devices/system/cpu" / f"cpu{cpus[0]}" / "cpuidle"
    try:
        states = sorted((p for p in base.iterdir() if p.name.startswith("state")), key=lambda p: p.name)
    except OSError:
        return []
    return [
        {"name": read_text(p / "name"), "latency_us": read_text(p / "latency"),
         "disabled": read_text(p / "disable")}
        for p in states
    ]


def irq_inventory(proc_root: Path, interface: str) -> list[dict[str, Any]]:
    interrupts = read_text(proc_root / "interrupts") or ""
    rows: list[dict[str, Any]] = []
    for line in interrupts.splitlines():
        if interface not in line:
            continue
        match = re.match(r"\s*(\d+):", line)
        if not match:
            continue
        irq = int(match.group(1))
        affinity = parse_cpu_list(read_text(proc_root / "irq" / str(irq) / "smp_affinity_list"))
        rows.append({"irq": irq, "affinity_cpus": affinity, "line": line.strip()})
    return rows


def nic_inventory(sys_root: Path, proc_root: Path, inspect_tools: bool) -> list[dict[str, Any]]:
    net_root = sys_root / "class/net"
    try:
        names = sorted(p.name for p in net_root.iterdir() if p.name != "lo")
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for name in names:
        base = net_root / name
        try:
            queues = list((base / "queues").iterdir())
        except OSError:
            queues = []
        rx = sorted((q for q in queues if q.name.startswith("rx-")), key=lambda q: q.name)
        tx = sorted((q for q in queues if q.name.startswith("tx-")), key=lambda q: q.name)
        try:
            driver = (base / "device/driver").resolve().name
        except OSError:
            driver = None
        row: dict[str, Any] = {
            "name": name,
            "operstate": read_text(base / "operstate"),
            "driver": driver,
            "rx_queues": len(rx),
            "tx_queues": len(tx),
            "rps_masks": [read_text(q / "rps_cpus") or "" for q in rx],
            "xps_masks": [read_text(q / "xps_cpus") or "" for q in tx],
            "irqs": irq_inventory(proc_root, name),
        }
        if inspect_tools:
            row["ethtool_driver"] = run_readonly(["ethtool", "-i", name])
            row["ethtool_coalesce"] = run_readonly(["ethtool", "-c", name])
            row["ethtool_channels"] = run_readonly(["ethtool", "-l", name])
            row["ethtool_ring"] = run_readonly(["ethtool", "-g", name])
        rows.append(row)
    return rows


def audit(policy: dict[str, Any], root: Path = Path("/"), resource_plan: dict[str, Any] | None = None) -> dict[str, Any]:
    sys_root, proc_root = root / "sys", root / "proc"
    cpus = allowed_cpus() if root == Path("/") else parse_cpu_list(read_text(sys_root / "devices/system/cpu/online"))
    topology = cpu_topology(sys_root, cpus)
    requirements = policy["hard_requirements"]
    hard_failures: list[str] = []
    if root == Path("/") and requirements.get("linux") is True and platform.system() != "Linux":
        hard_failures.append("linux_required")
    if len(cpus) < int(requirements["minimum_visible_cpus"]):
        hard_failures.append("outer_cpuset_below_eight_cpus")
    if topology["physical_cores"] < int(requirements["minimum_physical_cores"]):
        hard_failures.append("insufficient_physical_cores")
    if topology["maximum_threads_per_core"] > int(requirements["maximum_threads_per_core"]):
        hard_failures.append("smt_or_sibling_contention")

    hot_count = int(policy["hot_path"]["cpu_count"])
    hot = list((resource_plan or {}).get("hot_path_cpus") or cpus[-hot_count:])
    decision_cpu = hot[0] if hot else None
    cmdline = read_text(proc_root / "cmdline") or ""
    isolated = parse_cpu_list(read_text(sys_root / "devices/system/cpu/isolated")) or cmdline_cpu_arg(cmdline, "isolcpus")
    nohz = parse_cpu_list(read_text(sys_root / "devices/system/cpu/nohz_full")) or cmdline_cpu_arg(cmdline, "nohz_full")
    rcu = cmdline_cpu_arg(cmdline, "rcu_nocbs")
    baseline_failures: list[str] = []
    hot_set = set(hot)
    for name, observed in (("isolcpus", isolated), ("nohz_full", nohz), ("rcu_nocbs", rcu)):
        if not hot_set.issubset(observed):
            baseline_failures.append(f"hot_cpus_missing_from_{name}")

    thp = read_text(sys_root / "kernel/mm/transparent_hugepage/enabled")
    swappiness = read_text(proc_root / "sys/vm/swappiness")
    numa_balancing = read_text(proc_root / "sys/kernel/numa_balancing")
    nmi_watchdog = read_text(proc_root / "sys/kernel/nmi_watchdog")
    governors = cpu_governors(sys_root, hot)
    if policy["baseline_jitter_requirements"].get("transparent_hugepages_never") and thp and "[never]" not in thp:
        baseline_failures.append("transparent_hugepages_not_never")
    maximum_swap = int(policy["baseline_jitter_requirements"].get("swappiness_maximum", 1))
    if swappiness and swappiness.isdigit() and int(swappiness) > maximum_swap:
        baseline_failures.append("swappiness_above_target")
    if policy["baseline_jitter_requirements"].get("numa_balancing_disabled") and numa_balancing not in (None, "0"):
        baseline_failures.append("numa_balancing_enabled")
    if policy["baseline_jitter_requirements"].get("nmi_watchdog_disabled") and nmi_watchdog not in (None, "0"):
        baseline_failures.append("nmi_watchdog_enabled")
    if governors and policy["baseline_jitter_requirements"].get("performance_governor_when_available") and governors != ["performance"]:
        baseline_failures.append("hot_cpu_governor_not_performance")

    network = nic_inventory(sys_root, proc_root, inspect_tools=(root == Path("/")))
    network_failures: list[str] = []
    ena_rows = [row for row in network if row.get("driver") == "ena"]
    if not ena_rows and root == Path("/"):
        network_failures.append("ena_interface_not_observed")
    for row in ena_rows:
        if decision_cpu is not None and any(decision_cpu in irq["affinity_cpus"] for irq in row["irqs"]):
            network_failures.append("ena_irq_can_run_on_decision_cpu")
    irqbalance = proc_running(proc_root, "irqbalance")
    if irqbalance and policy["network_measurement"].get("manual_irq_policy_requires_irqbalance_off"):
        network_failures.append("irqbalance_running_manual_irq_policy_unproven")

    return {
        "schema": SCHEMA,
        "paper_only": True,
        "audit_only": True,
        "platform": {"system": platform.system(), "kernel": platform.release()},
        "cpu": topology,
        "hot_path_cpus": hot,
        "decision_cpu": decision_cpu,
        "scheduler_isolation": {
            "isolated_cpus": isolated,
            "nohz_full_cpus": nohz,
            "rcu_nocbs_cpus": rcu,
            "governors": governors,
            "idle_states": cpu_idle_states(sys_root, hot),
        },
        "memory": {
            "transparent_hugepage": thp,
            "swappiness": swappiness,
            "numa_online": read_text(sys_root / "devices/system/node/online"),
        },
        "kernel": {
            "clocksource": read_text(sys_root / "devices/system/clocksource/clocksource0/current_clocksource"),
            "nmi_watchdog": nmi_watchdog,
            "numa_balancing": numa_balancing,
            "irqbalance_running": irqbalance,
            "busy_poll_us": read_text(proc_root / "sys/net/core/busy_poll"),
            "busy_read_us": read_text(proc_root / "sys/net/core/busy_read"),
        },
        "network": network,
        "hard_failures": hard_failures,
        "baseline_failures": baseline_failures,
        "network_failures": sorted(set(network_failures)),
        "baseline_ready": not hard_failures and not baseline_failures,
        "network_ready": not network_failures,
        "hft_host_ready": not hard_failures and not baseline_failures and not network_failures,
        "benchmark_only_not_defaults": policy.get("benchmark_only_not_defaults", []),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=Path("config/v7_linux_hft_host_policy.json"))
    parser.add_argument("--resource-plan", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    policy = json.loads(args.policy.read_text(encoding="utf-8"))
    if policy.get("schema") != "polymarket_v7_linux_hft_host_policy_v1" or policy.get("paper_only") is not True:
        raise SystemExit("invalid Linux HFT host policy")
    if args.validate_only:
        print("Linux HFT host audit configuration PASS")
        return 0
    plan = json.loads(args.resource_plan.read_text()) if args.resource_plan else None
    result = audit(policy, resource_plan=plan)
    payload = json.dumps(result, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["hft_host_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
