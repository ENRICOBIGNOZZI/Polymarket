#!/usr/bin/env python3
"""Prepare deterministic Linux HFT baseline settings; NIC tuning remains A/B-gated."""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import v7_linux_hft_host_audit as host_audit

SCHEMA = "polymarket_v7_linux_hft_prepare_receipt_v1"


def cpu_list(values: list[int]) -> str:
    values = sorted(set(values))
    if not values:
        raise ValueError("non-empty CPU set required")
    ranges: list[str] = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = value
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def render_grub(hot: list[int]) -> str:
    cpus = cpu_list(hot)
    options = (
        f"isolcpus=domain,managed_irq,{cpus} "
        f"nohz_full={cpus} rcu_nocbs={cpus} transparent_hugepage=never"
    )
    return f'GRUB_CMDLINE_LINUX_DEFAULT="${{GRUB_CMDLINE_LINUX_DEFAULT}} {options}"\n'


def render_sysctl() -> str:
    return "vm.swappiness=1\nkernel.numa_balancing=0\nkernel.nmi_watchdog=0\n"


def render_governor_service() -> str:
    return """[Unit]
Description=Polymarket V7 HFT CPU governor baseline
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do [ ! -w "$f" ] || echo performance > "$f"; done'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
"""


def atomic_write(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def run(command: list[str]) -> None:
    value = subprocess.run(command, text=True, capture_output=True, check=False)
    if value.returncode != 0:
        detail = (value.stderr or value.stdout).strip()
        raise RuntimeError(f"command failed rc={value.returncode}: {' '.join(command)}: {detail}")


def service_active(name: str) -> bool:
    value = subprocess.run(["systemctl", "is-active", "--quiet", name], check=False)
    return value.returncode == 0


def validate(policy: dict, plan: dict) -> list[int]:
    if policy.get("schema") != "polymarket_v7_linux_hft_host_policy_v1":
        raise ValueError("invalid HFT host policy")
    if plan.get("schema") != "polymarket_v7_runtime_resource_plan_v2":
        raise ValueError("runtime resource plan v2 required")
    if plan.get("outer_cpuset_contract_satisfied") is not True:
        raise ValueError("outer cpuset contract is not satisfied")
    if plan.get("cpu_classes_disjoint") is not True:
        raise ValueError("runtime CPU classes are not disjoint")
    hot = [int(x) for x in plan.get("hot_path_cpus") or []]
    if len(hot) != int(policy["hot_path"]["cpu_count"]):
        raise ValueError("wrong HFT hot CPU count")
    return hot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=Path("config/v7_linux_hft_host_policy.json"))
    parser.add_argument("--resource-plan", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    policy = json.loads(args.policy.read_text(encoding="utf-8"))
    plan = json.loads(args.resource_plan.read_text(encoding="utf-8"))
    hot = validate(policy, plan)
    if args.validate_only:
        print(f"Linux HFT baseline configuration PASS hot={cpu_list(hot)}")
        return 0
    if not args.apply:
        raise SystemExit("--apply required for host mutation")
    if platform.system() != "Linux" or os.geteuid() != 0:
        raise SystemExit("root Linux host required")
    if service_active("polymarket-v7-paper.service"):
        raise SystemExit("PAPER runtime must be stopped before host preparation")

    pre = host_audit.audit(policy, resource_plan=plan)
    if pre["hard_failures"]:
        raise SystemExit("hard HFT host requirements failed: " + ",".join(pre["hard_failures"]))

    grub = Path("/etc/default/grub.d/99-polymarket-hft.cfg")
    sysctl = Path("/etc/sysctl.d/99-polymarket-hft.conf")
    governor = Path("/etc/systemd/system/polymarket-v7-hft-governor.service")
    atomic_write(grub, render_grub(hot))
    atomic_write(sysctl, render_sysctl())
    atomic_write(governor, render_governor_service())
    run(["sysctl", "--system"])
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "polymarket-v7-hft-governor.service"])
    run(["systemctl", "start", "polymarket-v7-hft-governor.service"])
    run(["update-grub"])

    receipt = {
        "schema": SCHEMA,
        "timestamp": int(time.time()),
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "hot_path_cpus": hot,
        "grub_file": str(grub),
        "sysctl_file": str(sysctl),
        "governor_service": str(governor),
        "irq_or_ena_tuning_applied": False,
        "reboot_required": True,
        "preparation_complete": True,
    }
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
