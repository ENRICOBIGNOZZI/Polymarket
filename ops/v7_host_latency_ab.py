#!/usr/bin/env python3
"""Reversible host-latency A/B for disabled London PAPER benchmark hosts."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import struct
import subprocess
import time
from pathlib import Path
from typing import Any

PROFILES = (
    {"name": "performance_governor", "governor": True},
    {"name": "cstate_dma_latency", "cstate": True},
    {"name": "ena_interrupt_moderation_zero", "coalesce": True},
    {"name": "irq_feed_affinity", "irq": True},
    {"name": "socket_busy_poll_50", "busy_poll_us": 50},
    {"name": "decision_affinity", "pin_decision": True},
    {
        "name": "combined_runtime_tuning",
        "governor": True,
        "cstate": True,
        "coalesce": True,
        "irq": True,
        "busy_poll_us": 50,
        "pin_decision": True,
    },
)


def run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    value = subprocess.run(argv, text=True, capture_output=True, check=False)
    if check and value.returncode != 0:
        raise RuntimeError(
            f"command failed rc={value.returncode}: {' '.join(argv)}: "
            f"{value.stderr.strip()[-1200:]}")
    return value


def default_interface() -> str:
    text = run(["ip", "route", "show", "default"]).stdout
    match = re.search(r"\bdev\s+(\S+)", text)
    if not match:
        raise RuntimeError("default interface unavailable")
    return match.group(1)


def service_active(name: str) -> bool:
    return run(["systemctl", "is-active", "--quiet", name], check=False).returncode == 0


def irq_inventory(interface: str) -> dict[int, str]:
    out: dict[int, str] = {}
    for line in Path("/proc/interrupts").read_text(
            encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"\s*(\d+):", line)
        if not match or interface not in line:
            continue
        irq = int(match.group(1))
        path = Path(f"/proc/irq/{irq}/smp_affinity_list")
        if path.exists():
            out[irq] = path.read_text().strip()
    return out


def parse_coalesce(text: str) -> dict[str, Any]:
    def one(name: str) -> int | None:
        m = re.search(rf"^{re.escape(name)}:\s*(\d+)", text, re.M)
        return int(m.group(1)) if m else None
    adaptive = re.search(
        r"Adaptive RX:\s*(on|off)\s+TX:\s*(on|off|n/a)", text, re.I)
    return {
        "adaptive_rx": adaptive.group(1).lower() if adaptive else None,
        "adaptive_tx": adaptive.group(2).lower() if adaptive else None,
        "rx_usecs": one("rx-usecs"),
        "tx_usecs": one("tx-usecs"),
    }


def governor_state(cpus: list[int]) -> dict[int, str]:
    out: dict[int, str] = {}
    for cpu in cpus:
        path = Path(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_governor")
        if path.exists():
            out[cpu] = path.read_text().strip()
    return out


def isolated_cpus() -> list[int]:
    cmd = Path("/proc/cmdline").read_text()
    match = re.search(r"(?:^|\s)isolcpus=([^\s]+)", cmd)
    if not match:
        return []
    cpus: set[int] = set()
    raw = match.group(1).split(",")
    for token in raw:
        token = token.strip()
        if not token or not token[0].isdigit():
            continue
        if "-" in token:
            left, right = token.split("-", 1)
            if left.isdigit() and right.isdigit():
                cpus.update(range(int(left), int(right) + 1))
        elif token.isdigit():
            cpus.add(int(token))
    return sorted(cpus)


def role_cpus() -> dict[str, Any]:
    allowed = sorted(os.sched_getaffinity(0))
    if len(allowed) < 4:
        raise RuntimeError("at least four schedulable CPUs required")
    chosen = allowed[-4:]
    return {
        "allowed": allowed,
        "decision": chosen[0],
        "feeds": chosen[1:],
        "isolated": isolated_cpus(),
    }


def capture(interface: str) -> dict[str, Any]:
    driver_text = run(["ethtool", "-i", interface]).stdout
    driver_match = re.search(r"^driver:\s*(\S+)", driver_text, re.M)
    driver = driver_match.group(1) if driver_match else "UNKNOWN"
    coalesce_raw = run(["ethtool", "-c", interface], check=False)
    roles = role_cpus()
    return {
        "interface": interface,
        "driver": driver,
        "coalesce": parse_coalesce(coalesce_raw.stdout)
            if coalesce_raw.returncode == 0 else None,
        "irqs": {str(k): v for k, v in irq_inventory(interface).items()},
        "irqbalance_active": service_active("irqbalance"),
        "roles": roles,
        "governors": {str(k): v for k, v in governor_state(roles["allowed"]).items()},
        "cpu_dma_latency_available": Path("/dev/cpu_dma_latency").exists(),
        "cmdline": Path("/proc/cmdline").read_text().strip(),
    }


def restore(state: dict[str, Any]) -> None:
    interface = state["interface"]
    coalesce = state.get("coalesce")
    if coalesce and coalesce.get("rx_usecs") is not None:
        cmd = ["ethtool", "-C", interface]
        if coalesce.get("adaptive_rx") in {"on", "off"}:
            cmd += ["adaptive-rx", coalesce["adaptive_rx"]]
        cmd += ["rx-usecs", str(coalesce["rx_usecs"])]
        if coalesce.get("tx_usecs") is not None:
            cmd += ["tx-usecs", str(coalesce["tx_usecs"])]
        run(cmd, check=False)
    for irq, affinity in (state.get("irqs") or {}).items():
        path = Path(f"/proc/irq/{irq}/smp_affinity_list")
        if path.exists():
            path.write_text(str(affinity) + "\n")
    if state.get("irqbalance_active"):
        run(["systemctl", "start", "irqbalance"], check=False)
    else:
        run(["systemctl", "stop", "irqbalance"], check=False)
    for cpu, governor in (state.get("governors") or {}).items():
        path = Path(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_governor")
        if path.exists():
            path.write_text(str(governor) + "\n")


def fingerprint(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "coalesce": state.get("coalesce"),
        "irqs": state.get("irqs"),
        "irqbalance_active": state.get("irqbalance_active"),
        "governors": state.get("governors"),
    }


def set_performance_governor(state: dict[str, Any]) -> None:
    if not state.get("governors"):
        raise RuntimeError("CPU governor sysfs unavailable")
    for cpu in state["roles"]["allowed"]:
        path = Path(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_governor")
        if not path.exists():
            continue
        choices = path.parent / "scaling_available_governors"
        available = choices.read_text().split() if choices.exists() else []
        if available and "performance" not in available:
            raise RuntimeError(f"performance governor unavailable on cpu{cpu}")
        path.write_text("performance\n")


def set_coalescing_zero(state: dict[str, Any]) -> None:
    if state.get("driver") != "ena":
        raise RuntimeError(f"ENA required, observed {state.get('driver')}")
    if state.get("coalesce") is None:
        raise RuntimeError("coalescing state unavailable")
    result = run([
        "ethtool", "-C", state["interface"],
        "adaptive-rx", "off", "rx-usecs", "0", "tx-usecs", "0",
    ], check=False)
    if result.returncode != 0:
        raise RuntimeError("ENA zero-coalescing unsupported: " + result.stderr.strip()[-500:])


def set_irq_affinity(state: dict[str, Any]) -> None:
    irqs = sorted(int(x) for x in state.get("irqs", {}))
    feeds = list(state["roles"]["feeds"])
    if not irqs:
        raise RuntimeError("interface IRQ inventory unavailable")
    run(["systemctl", "stop", "irqbalance"], check=False)
    for index, irq in enumerate(irqs):
        Path(f"/proc/irq/{irq}/smp_affinity_list").write_text(
            f"{feeds[index % len(feeds)]}\n")


def open_cstate_constraint(state: dict[str, Any]) -> int:
    path = Path("/dev/cpu_dma_latency")
    if not path.exists():
        raise RuntimeError("/dev/cpu_dma_latency unavailable")
    fd = os.open(path, os.O_RDWR)
    os.write(fd, struct.pack("i", 0))
    return fd


def run_probe(
    probe: Path, samples: int, interval_ms: int, busy_poll_us: int,
    pin_cpu: int | None) -> dict[str, Any]:
    argv = [
        str(probe), "--samples", str(samples), "--warmup", "8",
        "--interval-ms", str(interval_ms),
        "--socket-busy-poll-us", str(busy_poll_us),
    ]
    if pin_cpu is not None:
        argv = ["taskset", "-c", str(pin_cpu), *argv]
    result = run(argv)
    value = json.loads(result.stdout)
    if value.get("paper_only") is not True \
            or value.get("authenticated_execution") is not False \
            or value.get("real_order_submission") is not False:
        raise RuntimeError("probe safety boundary invalid")
    if int(value.get("socket_busy_poll_us", -1)) != busy_poll_us:
        raise RuntimeError("busy-poll provenance mismatch")
    return value


def metric(row: dict[str, Any]) -> tuple[int, int, int]:
    pair = row["parallel_persistent_legs"]["pair_completion_ns"]
    return int(pair["p99"]), int(pair["p999"]), int(
        row["parallel_persistent_legs"]["failures"])


def compare(candidate: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    cp99, cp999, cf = metric(candidate)
    bp99, bp999, bf = metric(baseline)
    return {
        "candidate_p99_ns": cp99,
        "baseline_p99_ns": bp99,
        "candidate_p999_ns": cp999,
        "baseline_p999_ns": bp999,
        "p99_improvement_pct": 100.0 * (bp99 - cp99) / bp99 if bp99 else None,
        "p999_improvement_pct": 100.0 * (bp999 - cp999) / bp999 if bp999 else None,
        "failure_delta": cf - bf,
        "promotion_candidate": cp99 <= bp99 * 0.90 and cp999 <= bp999 and cf <= bf,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=250)
    parser.add_argument("--interval-ms", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.expected_sha):
        raise SystemExit("exact SHA required")
    if not 50 <= args.samples <= 20_000:
        raise SystemExit("samples out of range")
    if not 0 <= args.interval_ms <= 5_000:
        raise SystemExit("interval out of range")
    if os.geteuid() != 0:
        raise SystemExit("root required")
    if not args.probe.is_file() or not os.access(args.probe, os.X_OK):
        raise SystemExit("probe executable required")
    if service_active("polymarket-v7-paper.service"):
        raise SystemExit("PAPER runtime must be stopped")

    lock_path = Path("/run/lock/polymarket-v7-host-latency-ab.lock")
    lock = lock_path.open("a+")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    interface = default_interface()
    original = capture(interface)
    result: dict[str, Any] = {
        "schema": "polymarket_v7_host_latency_ab_v1",
        "expected_sha": args.expected_sha,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "persistent_tuning": False,
        "original": original,
        "profiles": [],
    }

    try:
        for profile in PROFILES:
            restore(original)
            time.sleep(0.5)
            baseline = run_probe(
                args.probe, args.samples, args.interval_ms, 0, None)
            restore(original)
            cstate_fd: int | None = None
            try:
                if profile.get("governor"):
                    set_performance_governor(original)
                if profile.get("coalesce"):
                    set_coalescing_zero(original)
                if profile.get("irq"):
                    set_irq_affinity(original)
                if profile.get("cstate"):
                    cstate_fd = open_cstate_constraint(original)
                pin = int(original["roles"]["decision"]) \
                    if profile.get("pin_decision") else None
                candidate = run_probe(
                    args.probe, args.samples, args.interval_ms,
                    int(profile.get("busy_poll_us", 0)), pin)
                result["profiles"].append({
                    "profile": profile,
                    "supported": True,
                    "applied": capture(interface),
                    "baseline": baseline,
                    "candidate": candidate,
                    "comparison": compare(candidate, baseline),
                })
            except Exception as error:
                result["profiles"].append({
                    "profile": profile,
                    "supported": False,
                    "error": f"{type(error).__name__}: {error}",
                    "baseline": baseline,
                })
            finally:
                if cstate_fd is not None:
                    os.close(cstate_fd)
                restore(original)
                time.sleep(0.2)
        restored = capture(interface)
        result["restored"] = restored
        result["restored_exactly"] = fingerprint(restored) == fingerprint(original)
        if not result["restored_exactly"]:
            raise RuntimeError("host tuning rollback mismatch")
    finally:
        try:
            restore(original)
        except Exception:
            pass

    args.output.parent.mkdir(parents=True, exist_ok=True)
    temp = args.output.with_suffix(args.output.suffix + ".tmp")
    temp.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    temp.replace(args.output)
    print(f"result={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
