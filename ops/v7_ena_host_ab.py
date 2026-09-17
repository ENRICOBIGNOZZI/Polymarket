#!/usr/bin/env python3
"""Reversible ENA host-only latency A/B for disabled London PAPER hosts."""
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
    {"name": "interrupt_moderation_zero", "interrupt_zero": True},
    {"name": "irq_napi_feed_affinity", "irq_feed_affinity": True},
    {"name": "combined", "interrupt_zero": True, "irq_feed_affinity": True},
)


def run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    value = subprocess.run(argv, text=True, capture_output=True, check=False)
    if check and value.returncode != 0:
        raise RuntimeError(
            f"command failed rc={value.returncode}: {' '.join(argv)}: "
            f"{(value.stderr or value.stdout).strip()}")
    return value


def valid_sha(value: str) -> bool:
    return len(value) == 40 and all(ch in "0123456789abcdef" for ch in value)


def service_active(name: str) -> bool:
    return run(["systemctl", "is-active", "--quiet", name], check=False).returncode == 0


def default_interface() -> str:
    text = run(["ip", "route", "show", "default"]).stdout
    match = re.search(r"\bdev\s+(\S+)", text)
    if not match:
        raise RuntimeError("default network interface unavailable")
    return match.group(1)


def parse_coalesce(text: str) -> dict[str, Any]:
    adaptive = re.search(r"Adaptive RX:\s*(on|off)\s+TX:\s*(on|off|n/a)", text, re.I)
    rx = re.search(r"^rx-usecs:\s*(\d+)", text, re.M)
    tx = re.search(r"^tx-usecs:\s*(\d+)", text, re.M)
    if not adaptive or not rx or not tx:
        raise ValueError("required ENA coalescing fields unavailable")
    return {
        "adaptive_rx": adaptive.group(1).lower(),
        "adaptive_tx": adaptive.group(2).lower(),
        "rx_usecs": int(rx.group(1)),
        "tx_usecs": int(tx.group(1)),
    }


def irq_inventory(interface: str) -> dict[int, str]:
    rows: dict[int, str] = {}
    text = Path("/proc/interrupts").read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        if interface not in line:
            continue
        match = re.match(r"\s*(\d+):", line)
        if not match:
            continue
        irq = int(match.group(1))
        path = Path(f"/proc/irq/{irq}/smp_affinity_list")
        if path.is_file():
            rows[irq] = path.read_text(encoding="utf-8").strip()
    return rows


def load_roles(resource_plan: Path) -> dict[str, Any]:
    value = json.loads(resource_plan.read_text(encoding="utf-8"))
    hot = [int(x) for x in value.get("hot_path_cpus") or []]
    if value.get("schema") != "polymarket_v7_runtime_resource_plan_v2":
        raise ValueError("runtime resource plan v2 required")
    if value.get("outer_cpuset_contract_satisfied") is not True or len(hot) != 4:
        raise ValueError("four-core HFT CPU contract required")
    return {"decision": hot[0], "feeds": hot[1:], "hot": hot}


def capture(interface: str, roles: dict[str, Any]) -> dict[str, Any]:
    driver_raw = run(["ethtool", "-i", interface]).stdout
    match = re.search(r"^driver:\s*(\S+)", driver_raw, re.M)
    driver = match.group(1) if match else None
    if driver != "ena":
        raise RuntimeError(f"ENA required, observed driver={driver or 'UNKNOWN'}")
    coalesce_raw = run(["ethtool", "-c", interface]).stdout
    return {
        "interface": interface,
        "driver": driver,
        "coalesce": parse_coalesce(coalesce_raw),
        "irqs": {str(k): v for k, v in irq_inventory(interface).items()},
        "irqbalance_active": service_active("irqbalance"),
        "roles": roles,
    }


def restore(state: dict[str, Any]) -> None:
    interface = state["interface"]
    c = state["coalesce"]
    run(["ethtool", "-C", interface,
         "adaptive-rx", c["adaptive_rx"],
         "rx-usecs", str(c["rx_usecs"]),
         "tx-usecs", str(c["tx_usecs"])])
    for irq, affinity in state["irqs"].items():
        Path(f"/proc/irq/{irq}/smp_affinity_list").write_text(
            affinity + "\n", encoding="utf-8")
    if state["irqbalance_active"]:
        run(["systemctl", "start", "irqbalance"])
    else:
        run(["systemctl", "stop", "irqbalance"], check=False)


def apply_profile(state: dict[str, Any], profile: dict[str, Any]) -> None:
    interface = state["interface"]
    if profile.get("interrupt_zero"):
        run(["ethtool", "-C", interface,
             "adaptive-rx", "off", "rx-usecs", "0", "tx-usecs", "0"])
    if profile.get("irq_feed_affinity"):
        irqs = sorted(int(x) for x in state["irqs"])
        feeds = [int(x) for x in state["roles"]["feeds"]]
        if not irqs or len(feeds) != 3:
            raise RuntimeError("ENA IRQs and three feed CPUs required")
        run(["systemctl", "stop", "irqbalance"], check=False)
        for index, irq in enumerate(irqs):
            Path(f"/proc/irq/{irq}/smp_affinity_list").write_text(
                f"{feeds[index % len(feeds)]}\n", encoding="utf-8")


def mutable_fingerprint(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "coalesce": state["coalesce"],
        "irqs": state["irqs"],
        "irqbalance_active": state["irqbalance_active"],
    }


def run_probe(probe: Path, sha: str, region: str,
              samples: int, interval_ms: int) -> dict[str, Any]:
    value = run([str(probe), "--region", region, "--exact-code-sha", sha,
                 "--samples", str(samples), "--interval-ms", str(interval_ms)])
    parsed = json.loads(value.stdout)
    if parsed.get("exact_code_sha") != sha or parsed.get("region") != region:
        raise RuntimeError("latency probe provenance mismatch")
    return parsed


def metric(value: dict[str, Any], path: tuple[str, ...]) -> int:
    current: Any = value
    for key in path:
        current = current[key]
    return int(current)


def delta(candidate: dict[str, Any], baseline: dict[str, Any]) -> dict[str, int]:
    return {
        "total_p50_ns": metric(candidate, ("timings_ns", "total", "p50"))
            - metric(baseline, ("timings_ns", "total", "p50")),
        "total_p99_ns": metric(candidate, ("timings_ns", "total", "p99"))
            - metric(baseline, ("timings_ns", "total", "p99")),
        "total_p99_9_ns": metric(candidate, ("timings_ns", "total", "p99_9"))
            - metric(baseline, ("timings_ns", "total", "p99_9")),
        "first_byte_p99_ns": metric(candidate, ("timings_ns", "first_byte", "p99"))
            - metric(baseline, ("timings_ns", "first_byte", "p99")),
        "failed_samples_delta": int(candidate.get("failed_samples", 0))
            - int(baseline.get("failed_samples", 0)),
    }


def preflight(args: argparse.Namespace) -> dict[str, Any]:
    if os.name != "posix" or not Path("/proc").is_dir() or os.geteuid() != 0:
        raise RuntimeError("root Linux host required")
    if not valid_sha(args.expected_sha):
        raise RuntimeError("exact lowercase 40-char SHA required")
    if service_active("polymarket-v7-paper.service"):
        raise RuntimeError("PAPER runtime must remain stopped during ENA A/B")
    observed = run(["git", "-C", str(args.app), "rev-parse", "HEAD"]).stdout.strip()
    if observed != args.expected_sha:
        raise RuntimeError("repository SHA mismatch")
    if not args.probe.is_file() or not os.access(args.probe, os.X_OK):
        raise RuntimeError("latency probe executable required")
    return load_roles(args.resource_plan)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--region-label", required=True)
    parser.add_argument("--app", type=Path, default=Path("/home/ubuntu/polymarket"))
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--resource-plan", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=120)
    parser.add_argument("--interval-ms", type=int, default=500)
    parser.add_argument("--settle-seconds", type=float, default=1.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 10 <= args.samples <= 100000 or not 0 <= args.interval_ms <= 60000:
        raise SystemExit("benchmark arguments out of range")
    roles = preflight(args)
    interface = default_interface()
    baseline = capture(interface, roles)
    if not baseline["irqs"]:
        raise RuntimeError("ENA queue IRQs were not observable")
    result: dict[str, Any] = {
        "schema": "polymarket_v7_ena_host_ab_v1",
        "timestamp_ns": time.time_ns(),
        "exact_code_sha": args.expected_sha,
        "region": args.region_label,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "persistent_tuning": False,
        "baseline_host_state": baseline,
        "runs": [],
    }
    restored = False
    try:
        for profile in PROFILES:
            restore(baseline)
            time.sleep(args.settle_seconds)
            paired = run_probe(args.probe, args.expected_sha, args.region_label,
                               args.samples, args.interval_ms)
            restore(baseline)
            apply_profile(baseline, profile)
            applied = capture(interface, roles)
            time.sleep(args.settle_seconds)
            candidate = run_probe(args.probe, args.expected_sha, args.region_label,
                                  args.samples, args.interval_ms)
            change = delta(candidate, paired)
            result["runs"].append({
                "profile": profile,
                "baseline": paired,
                "candidate": candidate,
                "applied_host_state": applied,
                "delta_candidate_minus_baseline": change,
                "improved_tail_without_failures": (
                    change["total_p99_ns"] < 0
                    and change["total_p99_9_ns"] < 0
                    and change["failed_samples_delta"] <= 0),
            })
    finally:
        restore(baseline)
        restored_state = capture(interface, roles)
        restored = mutable_fingerprint(restored_state) == mutable_fingerprint(baseline)
        result["restored_host_state"] = restored_state
        result["restored_exactly"] = restored
    if not restored:
        raise RuntimeError("ENA host A/B rollback verification failed")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(args.output.suffix + ".tmp")
    tmp.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    tmp.replace(args.output)
    print(f"result={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
