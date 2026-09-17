#!/usr/bin/env python3
"""Read-only TLS-connectivity probe for public V7 venue endpoints."""
from __future__ import annotations

import argparse
import json
import math
import socket
import ssl
import subprocess
import time
from pathlib import Path

TARGETS = (
    ("polymarket_clob", "clob.polymarket.com", 443),
    ("polymarket_ws", "ws-subscriptions-clob.polymarket.com", 443),
    ("binance_spot", "stream.binance.com", 9443),
    ("binance_usdm", "fstream.binance.com", 443),
    ("coinbase_spot", "ws-feed.exchange.coinbase.com", 443),
    ("bybit", "stream.bybit.com", 443),
    ("deribit", "www.deribit.com", 443),
)


def exact_sha(value: str) -> bool:
    return len(value) == 40 and all(c in "0123456789abcdef" for c in value)


def distribution(values: list[float]) -> dict[str, float]:
    values = sorted(values)
    if not values:
        return {}
    def q(probability: float) -> float:
        index = max(0, min(len(values) - 1, math.ceil(probability * len(values)) - 1))
        return values[index]
    return {name: round(q(p), 3) for name, p in (
        ("p50", .5), ("p90", .9), ("p95", .95), ("p99", .99), ("max", 1.0)
    )}

def probe(host: str, port: int, samples: int) -> dict[str, object]:
    dns: list[float] = []
    tcp: list[float] = []
    tls: list[float] = []
    total: list[float] = []
    errors: list[str] = []
    ips: dict[str, int] = {}
    context = ssl.create_default_context()
    for _ in range(samples):
        start = time.perf_counter_ns()
        raw_socket = None
        try:
            dns_start = time.perf_counter_ns()
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            dns.append((time.perf_counter_ns() - dns_start) / 1e6)
            family, address = infos[0][0], infos[0][4]
            raw_socket = socket.socket(family, socket.SOCK_STREAM)
            raw_socket.settimeout(5)
            connect_start = time.perf_counter_ns()
            raw_socket.connect(address)
            tcp.append((time.perf_counter_ns() - connect_start) / 1e6)
            ip = raw_socket.getpeername()[0]
            ips[ip] = ips.get(ip, 0) + 1
            tls_start = time.perf_counter_ns()
            wrapped = context.wrap_socket(raw_socket, server_hostname=host)
            tls.append((time.perf_counter_ns() - tls_start) / 1e6)
            total.append((time.perf_counter_ns() - start) / 1e6)
            wrapped.close()
            raw_socket = None
        except Exception as exc:
            errors.append(type(exc).__name__ + ":" + str(exc))
            if raw_socket is not None:
                try:
                    raw_socket.close()
                except OSError:
                    pass
    return {
        "success": len(total), "failure": len(errors), "ips": ips,
        "dns_ms": distribution(dns), "tcp_ms": distribution(tcp),
        "tls_ms": distribution(tls), "total_ms": distribution(total),
        "errors": errors[:10],
    }


def current_sha(app: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(app), "rev-parse", "HEAD"], text=True,
        stderr=subprocess.DEVNULL, timeout=5,
    ).strip()

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not exact_sha(args.expected_sha):
        raise SystemExit("exact lowercase SHA required")
    if not 1 <= args.samples <= 1000:
        raise SystemExit("samples must be in [1,1000]")
    observed_sha = current_sha(args.app)
    if observed_sha != args.expected_sha:
        raise SystemExit(f"exact SHA mismatch: {observed_sha} != {args.expected_sha}")
    rows = {name: probe(host, port, args.samples) for name, host, port in TARGETS}
    result = {
        "schema": "polymarket_v7_external_tls_probe_v1",
        "region": args.region, "exact_code_sha": args.expected_sha,
        "samples_per_target": args.samples, "targets": rows,
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "authorizes_live_execution": False,
        "measurement_scope": "PUBLIC_DNS_TCP_TLS_CONNECTIVITY_ONLY",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
