#!/usr/bin/env python3
"""Reversible ENA low-latency A/B on a disabled London PAPER benchmark host."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

PROFILES = (
    {"name": "busy_poll_50", "busy_poll_us": 50},
    {"name": "interrupt_moderation_zero", "interrupt_zero": True},
    {"name": "irq_napi_feed_affinity", "irq_feed_affinity": True},
    {"name": "combined", "busy_poll_us": 50, "interrupt_zero": True,
     "irq_feed_affinity": True},
)


def run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    value = subprocess.run(argv, text=True, capture_output=True, check=False)
    if check and value.returncode != 0:
        raise RuntimeError(f"command failed rc={value.returncode}: {' '.join(argv)}: {value.stderr.strip()}")
    return value


def parse_coalesce(text: str) -> dict[str, Any]:
    adaptive = re.search(r"Adaptive RX:\s*(on|off)\s+TX:\s*(on|off)", text, re.I)
    rx = re.search(r"^rx-usecs:\s*(\d+)", text, re.M)
    tx = re.search(r"^tx-usecs:\s*(\d+)", text, re.M)
    if not adaptive or not rx or not tx:
        raise ValueError("required ENA coalescing fields unavailable")
    return {"adaptive_rx": adaptive.group(1).lower(), "adaptive_tx": adaptive.group(2).lower(),
            "rx_usecs": int(rx.group(1)), "tx_usecs": int(tx.group(1))}


def default_interface() -> str:
    text = run(["ip", "route", "show", "default"]).stdout
    match = re.search(r"\bdev\s+(\S+)", text)
    if not match:
        raise RuntimeError("default network interface unavailable")
    return match.group(1)


def irq_inventory(interface: str) -> dict[int, str]:
    result: dict[int, str] = {}
    for line in Path("/proc/interrupts").read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"\s*(\d+):", line)
        if match and interface in line:
            irq = int(match.group(1))
            path = Path(f"/proc/irq/{irq}/smp_affinity_list")
            if path.exists():
                result[irq] = path.read_text(encoding="utf-8").strip()
    return result


def select_roles(allowed: list[int]) -> dict[str, Any]:
    allowed = sorted(allowed)
    if len(allowed) < 4:
        raise RuntimeError("at least four allowed CPUs required for HFT role placement")
    selected = allowed[-4:]
    return {"allowed": allowed, "decision": selected[0], "feeds": selected[1:]}


def role_cpus() -> dict[str, Any]:
    return select_roles(list(os.sched_getaffinity(0)))


def service_active(name: str) -> bool:
    return run(["systemctl", "is-active", "--quiet", name], check=False).returncode == 0


def capture(interface: str) -> dict[str, Any]:
    driver = run(["ethtool", "-i", interface]).stdout
    match = re.search(r"^driver:\s*(\S+)", driver, re.M)
    if not match or match.group(1) != "ena":
        raise RuntimeError(f"ENA required, observed driver={match.group(1) if match else 'UNKNOWN'}")
    coalesce_raw = run(["ethtool", "-c", interface]).stdout
    return {
        "interface": interface,
        "driver": driver,
        "coalesce": parse_coalesce(coalesce_raw),
        "coalesce_raw": coalesce_raw,
        "irqs": {str(k): v for k, v in irq_inventory(interface).items()},
        "irqbalance_active": service_active("irqbalance"),
        "roles": role_cpus(),
        "busy_poll_sysctl": Path("/proc/sys/net/core/busy_poll").read_text().strip()
            if Path("/proc/sys/net/core/busy_poll").exists() else None,
        "busy_read_sysctl": Path("/proc/sys/net/core/busy_read").read_text().strip()
            if Path("/proc/sys/net/core/busy_read").exists() else None,
    }


def restore(state: dict[str, Any]) -> None:
    interface = state["interface"]
    c = state["coalesce"]
    run(["ethtool", "-C", interface,
         "adaptive-rx", c["adaptive_rx"],
         "rx-usecs", str(c["rx_usecs"]), "tx-usecs", str(c["tx_usecs"])])
    for irq, affinity in state["irqs"].items():
        Path(f"/proc/irq/{irq}/smp_affinity_list").write_text(affinity + "\n", encoding="utf-8")
    if state["irqbalance_active"]:
        run(["systemctl", "start", "irqbalance"])
    else:
        run(["systemctl", "stop", "irqbalance"], check=False)


def apply_profile(state: dict[str, Any], profile: dict[str, Any]) -> None:
    interface = state["interface"]
    if profile.get("interrupt_zero"):
        run(["ethtool", "-C", interface, "adaptive-rx", "off",
             "rx-usecs", "0", "tx-usecs", "0"])
    if profile.get("irq_feed_affinity"):
        irqs = sorted(int(x) for x in state["irqs"])
        feeds = list(state["roles"]["feeds"])
        if not irqs:
            raise RuntimeError("no interface IRQs available for NAPI/IRQ placement")
        run(["systemctl", "stop", "irqbalance"], check=False)
        for index, irq in enumerate(irqs):
            Path(f"/proc/irq/{irq}/smp_affinity_list").write_text(
                f"{feeds[index % len(feeds)]}\n", encoding="utf-8")


def mutable_fingerprint(state: dict[str, Any]) -> dict[str, Any]:
    return {"coalesce": state["coalesce"], "irqs": state["irqs"],
            "irqbalance_active": state["irqbalance_active"]}


def run_probe(probe: Path, sha: str, region: str, samples: int, interval_ms: int,
              busy_poll_us: int) -> dict[str, Any]:
    value = run([str(probe), "--region", region, "--exact-code-sha", sha,
                 "--samples", str(samples), "--interval-ms", str(interval_ms),
                 "--socket-busy-poll-us", str(busy_poll_us)])
    parsed = json.loads(value.stdout)
    if parsed.get("exact_code_sha") != sha or parsed.get("region") != region:
        raise RuntimeError("probe provenance mismatch")
    if parsed.get("socket_busy_poll_us") != busy_poll_us:
        raise RuntimeError("probe busy-poll provenance mismatch")
    return parsed


def delta(candidate: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    def get(metric: str, q: str) -> int:
        return int(candidate["timings_ns"][metric][q]) - int(baseline["timings_ns"][metric][q])
    return {
        "total_p50_ns": get("total", "p50"), "total_p99_ns": get("total", "p99"),
        "total_p99_9_ns": get("total", "p99_9"),
        "first_byte_p99_ns": get("first_byte", "p99"),
        "failed_samples_delta": int(candidate["failed_samples"]) - int(baseline["failed_samples"]),
    }


def valid_sha(value: str) -> bool:
    return len(value) == 40 and all(ch in "0123456789abcdef" for ch in value)


def preflight(args: argparse.Namespace) -> None:
    if os.name != "posix" or not Path("/proc").is_dir():
        raise RuntimeError("Linux /proc host required")
    if os.geteuid() != 0:
        raise RuntimeError("root required for reversible ENA tuning")
    if not valid_sha(args.expected_sha):
        raise RuntimeError("exact lowercase 40-char SHA required")
    if not args.probe.is_file() or not os.access(args.probe, os.X_OK):
        raise RuntimeError("latency probe executable required")
    if service_active("polymarket-v7-paper.service"):
        raise RuntimeError("PAPER runtime must remain stopped during ENA A/B")
    observed = run(["git", "-C", str(args.app), "rev-parse", "HEAD"]).stdout.strip()
    if observed != args.expected_sha:
        raise RuntimeError("repository SHA mismatch")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--region-label", required=True)
    parser.add_argument("--app", type=Path, default=Path("/home/ubuntu/polymarket"))
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=120)
    parser.add_argument("--interval-ms", type=int, default=500)
    parser.add_argument("--settle-seconds", type=float, default=1.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 10 or args.samples > 100000:
        raise SystemExit("samples out of range")
    if args.interval_ms < 0 or args.interval_ms > 60000:
        raise SystemExit("interval-ms out of range")
    if args.settle_seconds < 0 or args.settle_seconds > 30:
        raise SystemExit("settle-seconds out of range")
    preflight(args)
    interface = default_interface()
    baseline_state = capture(interface)
    if not baseline_state["irqs"]:
        raise RuntimeError("ENA queue IRQs were not observable")
    result: dict[str, Any] = {
        "schema": "polymarket_v7_ena_low_latency_ab_v1",
        "timestamp_ns": time.time_ns(),
        "exact_code_sha": args.expected_sha,
        "region": args.region_label,
        "interface": interface,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "persistent_tuning": False,
        "baseline_host_state": baseline_state,
        "runs": [],
    }
    restored = False
    try:
        for profile in PROFILES:
            restore(baseline_state)
            time.sleep(args.settle_seconds)
            paired_baseline = run_probe(
                args.probe, args.expected_sha, args.region_label,
                args.samples, args.interval_ms, 0)
            restore(baseline_state)
            apply_profile(baseline_state, profile)
            applied = capture(interface)
            time.sleep(args.settle_seconds)
            candidate = run_probe(
                args.probe, args.expected_sha, args.region_label,
                args.samples, args.interval_ms, int(profile.get("busy_poll_us", 0)))
            change = delta(candidate, paired_baseline)
            result["runs"].append({
                "profile": profile,
                "baseline": paired_baseline,
                "candidate": candidate,
                "applied_host_state": applied,
                "delta_candidate_minus_baseline": change,
                "improved_tail_without_failures": (
                    change["total_p99_ns"] < 0
                    and change["total_p99_9_ns"] < 0
                    and change["failed_samples_delta"] <= 0
                ),
            })
    finally:
        restore(baseline_state)
        restored_state = capture(interface)
        restored = mutable_fingerprint(restored_state) == mutable_fingerprint(baseline_state)
        result["restored_host_state"] = restored_state
        result["restored_exactly"] = restored
    if not restored:
        raise RuntimeError("ENA A/B rollback verification failed")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(f"result={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
