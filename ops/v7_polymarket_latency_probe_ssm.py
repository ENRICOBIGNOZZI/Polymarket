#!/usr/bin/env python3
"""Read-only London latency profiler for Polymarket PAPER research.

Measures public network/TLS/HTTP-WebSocket handshake timing and recent native
signal/decision timing. It never authenticates, submits, cancels, or modifies
orders. Actual order-path/venue-ack latency remains explicitly unmeasured.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys

from v7_direct_action_research_ssm import INSTANCE_RE, remote_context
from v7_london_ssm_deploy import REGION, run

SCHEMA = "polymarket_v7_london_latency_probe_v1"


def load_request(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "schema", "version", "request_id", "instance_id", "samples",
        "recent_window_seconds", "output_directory", "paper_only",
        "authenticated_execution", "real_order_submission",
        "real_capital_at_risk",
    }
    if set(value) != required:
        raise ValueError("unexpected latency probe request fields")
    if value["schema"] != "polymarket_v7_london_latency_probe_request_v1":
        raise ValueError("invalid latency probe request schema")
    if value["version"] != 1:
        raise ValueError("invalid latency probe request version")
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", value["request_id"]):
        raise ValueError("invalid latency probe request id")
    if not INSTANCE_RE.fullmatch(value["instance_id"]):
        raise ValueError("invalid instance id")
    if not isinstance(value["samples"], int) or not 5 <= value["samples"] <= 100:
        raise ValueError("latency samples outside bounded range")
    if (
        not isinstance(value["recent_window_seconds"], int)
        or not 60 <= value["recent_window_seconds"] <= 3600
    ):
        raise ValueError("invalid recent evidence window")
    if not re.fullmatch(
        r"docs/research/london-latency-[0-9]{4}-[0-9]{2}-[0-9]{2}",
        value["output_directory"],
    ):
        raise ValueError("invalid latency output directory")
    if not (
        value["paper_only"] is True
        and value["authenticated_execution"] is False
        and value["real_order_submission"] is False
        and value["real_capital_at_risk"] is False
    ):
        raise ValueError("PAPER-only latency probe contract violated")
    return value


def remote_probe(instance: str, context: dict, request: dict) -> dict:
    root = context["run_root"]
    samples = int(request["samples"])
    window = int(request["recent_window_seconds"])
    command = r"""python3 - __ROOT__ __SAMPLES__ __WINDOW__ <<'PY'
import gzip,json,math,socket,ssl,sys,time
from pathlib import Path

root=Path(sys.argv[1])
samples=int(sys.argv[2])
window_seconds=int(sys.argv[3])
hosts=[
  ("clob_rest","clob.polymarket.com","/"),
  ("clob_market_ws","ws-subscriptions-clob.polymarket.com","/ws/market"),
]

def quantile(values,p):
    values=sorted(float(x) for x in values if math.isfinite(float(x)))
    if not values:
        return None
    i=max(0,min(len(values)-1,math.ceil(p*len(values))-1))
    return values[i]

def stats(values):
    values=[float(x) for x in values if math.isfinite(float(x))]
    return {
      "count":len(values),
      "min":min(values) if values else None,
      "p50":quantile(values,.50),
      "p90":quantile(values,.90),
      "p99":quantile(values,.99),
      "max":max(values) if values else None,
      "mean":sum(values)/len(values) if values else None,
    }

network={}
ctx=ssl.create_default_context()
for label,host,path in hosts:
    dns_ms=[]; tcp_ms=[]; tls_ms=[]; first_byte_ms=[]; total_ms=[]
    failures=[]
    for _ in range(samples):
        started=time.perf_counter_ns()
        try:
            t=time.perf_counter_ns()
            infos=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
            dns_ms.append((time.perf_counter_ns()-t)/1e6)
            address=infos[0][4]
            family=infos[0][0]

            raw=socket.socket(family,socket.SOCK_STREAM)
            raw.settimeout(3.0)
            t=time.perf_counter_ns()
            raw.connect(address)
            tcp_ms.append((time.perf_counter_ns()-t)/1e6)

            t=time.perf_counter_ns()
            sock=ctx.wrap_socket(raw,server_hostname=host)
            tls_ms.append((time.perf_counter_ns()-t)/1e6)

            key="dGhlIHNhbXBsZSBub25jZQ=="
            if label.endswith("_ws"):
                request=(
                  f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
                  "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
                )
            else:
                request=(
                  f"HEAD {path} HTTP/1.1\r\nHost: {host}\r\n"
                  "Connection: close\r\nUser-Agent: polymarket-paper-latency-probe\r\n\r\n"
                )
            t=time.perf_counter_ns()
            sock.sendall(request.encode("ascii"))
            first=sock.recv(1)
            first_byte_ms.append((time.perf_counter_ns()-t)/1e6)
            if not first:
                failures.append("EMPTY_RESPONSE")
            sock.close()
            total_ms.append((time.perf_counter_ns()-started)/1e6)
        except Exception as exc:
            failures.append(type(exc).__name__)
            try:
                raw.close()
            except Exception:
                pass
        time.sleep(.02)
    network[label]={
      "host":host,
      "samples_requested":samples,
      "dns_ms":stats(dns_ms),
      "tcp_connect_ms":stats(tcp_ms),
      "tls_handshake_ms":stats(tls_ms),
      "request_to_first_byte_ms":stats(first_byte_ms),
      "cold_connection_total_ms":stats(total_ms),
      "failures":dict((name,failures.count(name)) for name in sorted(set(failures))),
    }

cutoff=time.time_ns()-window_seconds*1_000_000_000
hft=root/"research/hft_permanent"
candidates=[]
for folder in (hft/"compact",hft/"compact_closed"):
    if not folder.is_dir() or folder.is_symlink():
        continue
    for path in folder.glob("*.jsonl*"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            st=path.stat()
        except OSError:
            continue
        if st.st_mtime_ns>=cutoff:
            candidates.append((st.st_mtime_ns,st.st_size,path))
candidates.sort(reverse=True)
selected=[]; total=0; truncated=False
for _,size,path in candidates:
    if len(selected)>=96 or total+size>128*1024*1024:
        truncated=True
        continue
    selected.append(path); total+=size

signal_age=[]; decision_compute=[]; assumed_transport=[]; venue_delay=[]
kind2=0
for path in selected:
    opener=gzip.open if str(path).endswith(".gz") else open
    try:
        with opener(path,"rt",encoding="utf-8") as stream:
            for line in stream:
                try:
                    row=json.loads(line)
                except ValueError:
                    continue
                if row.get("schema")!="polymarket_v7_native_observation_v1" or row.get("kind")!=2:
                    continue
                wall=row.get("decision_wall_ns")
                if not isinstance(wall,int) or wall<cutoff:
                    continue
                kind2+=1
                v=row.get("signal_age_ns")
                if isinstance(v,(int,float)) and not isinstance(v,bool) and v>=0:
                    signal_age.append(float(v)/1e6)
                v=row.get("decision_compute_ns")
                if isinstance(v,(int,float)) and not isinstance(v,bool) and v>=0:
                    decision_compute.append(float(v)/1e6)
                v=row.get("paper_assumed_transport_delay_ns")
                if isinstance(v,int) and v>=0:
                    assumed_transport.append(v/1e6)
                v=row.get("paper_venue_delay_ns")
                if isinstance(v,int) and v>=0:
                    venue_delay.append(v/1e6)
    except (OSError,EOFError):
        truncated=True

print("LATENCY_PROBE="+json.dumps({
  "schema":"polymarket_v7_london_latency_probe_v1",
  "paper_only":True,
  "authenticated_execution":False,
  "real_order_submission":False,
  "real_capital_at_risk":False,
  "network":network,
  "recent_native":{
    "window_seconds":window_seconds,
    "kind2_rows":kind2,
    "signal_age_ms":stats(signal_age),
    "decision_compute_ms":stats(decision_compute),
    "paper_assumed_transport_delay_ms":stats(assumed_transport),
    "paper_venue_delay_ms":stats(venue_delay),
    "scanned_files":len(selected),
    "scanned_bytes":total,
    "scan_truncated":truncated,
  },
  "order_path_latency_measured":False,
  "network_probe_only":True,
  "interpretation":(
    "PUBLIC_NETWORK_HANDSHAKE_AND_NATIVE_COMPUTE_ONLY;"
    "DO_NOT_TREAT_AS_ORDER_ACK_OR_ONE_WAY_VENUE_LATENCY"
  ),
},sort_keys=True,separators=(",",":")))
PY"""
    command = (
        command.replace("__ROOT__", root)
        .replace("__SAMPLES__", str(samples))
        .replace("__WINDOW__", str(window))
    )
    stdout, _ = run(REGION, instance, command, 300)
    line = next(
        item for item in stdout.splitlines()
        if item.startswith("LATENCY_PROBE=")
    )
    result = json.loads(line.split("=", 1)[1])
    if (
        result.get("paper_only") is not True
        or result.get("authenticated_execution") is not False
        or result.get("real_order_submission") is not False
        or result.get("order_path_latency_measured") is not False
    ):
        raise RuntimeError("latency probe safety receipt mismatch")
    return result


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: v7_polymarket_latency_probe_ssm.py REQUEST")
    repo = Path(__file__).resolve().parents[1]
    request = load_request(Path(sys.argv[1]))
    context = remote_context(request["instance_id"])
    result = remote_probe(request["instance_id"], context, request)
    output = repo / request["output_directory"] / "latency.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print("LATENCY_PROBE_OUTPUT=" + str(output.relative_to(repo)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
