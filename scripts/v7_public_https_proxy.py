#!/usr/bin/env python3
"""Loopback HTTPS CONNECT proxy using public DNS for V7 PAPER public-data traffic.

The dedicated PAPER server's ISP resolver can DNS-filter Polymarket public
endpoints. This process never terminates TLS and never handles credentials: it
only resolves the CONNECT host through public DNS-over-TCP and relays the
encrypted byte stream. Binding is loopback-only.
"""
from __future__ import annotations

import argparse
import ipaddress
import random
import select
import socket
import struct
import threading
import time
from typing import Iterable

DEFAULT_DNS = ("1.1.1.1", "8.8.8.8")
CACHE_TTL_SECONDS = 60.0


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise OSError("unexpected EOF")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _qname(host: str) -> bytes:
    labels = host.rstrip(".").split(".")
    if not labels or any(not label or len(label.encode("idna")) > 63 for label in labels):
        raise ValueError("invalid DNS hostname")
    out = bytearray()
    for label in labels:
        encoded = label.encode("idna")
        out.append(len(encoded))
        out.extend(encoded)
    out.append(0)
    return bytes(out)


def _skip_name(packet: bytes, offset: int) -> int:
    while True:
        if offset >= len(packet):
            raise ValueError("truncated DNS name")
        length = packet[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(packet):
                raise ValueError("truncated DNS pointer")
            return offset + 2
        offset += 1
        if length == 0:
            return offset
        offset += length
        if offset > len(packet):
            raise ValueError("truncated DNS label")


def _dns_a(host: str, server: str, timeout: float = 3.0) -> list[str]:
    txid = random.randrange(0, 65536)
    query = struct.pack("!HHHHHH", txid, 0x0100, 1, 0, 0, 0) + _qname(host) + struct.pack("!HH", 1, 1)
    with socket.create_connection((server, 53), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(struct.pack("!H", len(query)) + query)
        size = struct.unpack("!H", _recv_exact(sock, 2))[0]
        packet = _recv_exact(sock, size)
    if len(packet) < 12:
        raise ValueError("short DNS response")
    rid, flags, qdcount, ancount, _, _ = struct.unpack("!HHHHHH", packet[:12])
    if rid != txid or flags & 0x000F:
        raise ValueError("DNS response failure")
    offset = 12
    for _ in range(qdcount):
        offset = _skip_name(packet, offset)
        offset += 4
    answers: list[str] = []
    for _ in range(ancount):
        offset = _skip_name(packet, offset)
        if offset + 10 > len(packet):
            break
        rtype, rclass, _ttl, rdlength = struct.unpack("!HHIH", packet[offset : offset + 10])
        offset += 10
        rdata = packet[offset : offset + rdlength]
        offset += rdlength
        if rtype == 1 and rclass == 1 and len(rdata) == 4:
            answers.append(socket.inet_ntoa(rdata))
    return list(dict.fromkeys(answers))


class PublicResolver:
    def __init__(self, servers: Iterable[str]) -> None:
        self.servers = tuple(str(ipaddress.ip_address(server)) for server in servers)
        self._cache: dict[str, tuple[float, list[str]]] = {}
        self._lock = threading.Lock()

    def resolve(self, host: str) -> list[str]:
        try:
            ipaddress.ip_address(host)
            return [host]
        except ValueError:
            pass
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(host)
            if cached and cached[0] > now:
                return list(cached[1])
        errors: list[str] = []
        for server in self.servers:
            try:
                answers = _dns_a(host, server)
                if answers:
                    with self._lock:
                        self._cache[host] = (now + CACHE_TTL_SECONDS, answers)
                    return answers
            except Exception as exc:  # noqa: BLE001 - diagnostic aggregation
                errors.append(f"{server}:{exc}")
        raise OSError(f"public DNS failed for {host}: {'; '.join(errors)}")


def _relay(client: socket.socket, upstream: socket.socket) -> None:
    sockets = [client, upstream]
    while sockets:
        readable, _, _ = select.select(sockets, [], [], 30.0)
        if not readable:
            continue
        for source in readable:
            target = upstream if source is client else client
            data = source.recv(65536)
            if not data:
                return
            target.sendall(data)


def _parse_connect(header: bytes) -> tuple[str, int]:
    first = header.split(b"\r\n", 1)[0].decode("ascii", "strict")
    parts = first.split()
    if len(parts) != 3 or parts[0].upper() != "CONNECT":
        raise ValueError("CONNECT required")
    authority = parts[1]
    if authority.startswith("["):
        closing = authority.find("]")
        if closing < 0:
            raise ValueError("invalid CONNECT authority")
        host = authority[1:closing]
        port = int(authority[closing + 2 :]) if authority[closing + 1 :].startswith(":") else 443
    else:
        host, separator, raw_port = authority.rpartition(":")
        if not separator:
            host, raw_port = authority, "443"
        port = int(raw_port)
    if not host or port != 443:
        raise ValueError("only HTTPS CONNECT port 443 is allowed")
    return host, port


def _handle(client: socket.socket, resolver: PublicResolver) -> None:
    upstream: socket.socket | None = None
    try:
        client.settimeout(10.0)
        header = bytearray()
        while b"\r\n\r\n" not in header:
            chunk = client.recv(4096)
            if not chunk:
                return
            header.extend(chunk)
            if len(header) > 65536:
                raise ValueError("proxy header too large")
        host, port = _parse_connect(bytes(header))
        last_error: Exception | None = None
        for address in resolver.resolve(host):
            try:
                upstream = socket.create_connection((address, port), timeout=8.0)
                break
            except OSError as exc:
                last_error = exc
        if upstream is None:
            raise OSError(f"unable to connect upstream {host}: {last_error}")
        client.sendall(b"HTTP/1.1 200 Connection Established\r\nProxy-Agent: polymarket-v7-public-dns\r\n\r\n")
        client.settimeout(None)
        upstream.settimeout(None)
        _relay(client, upstream)
    except Exception as exc:  # noqa: BLE001 - proxy must fail one connection, not process
        try:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except OSError:
            pass
        print(f"v7_public_https_proxy connection_error={exc}", flush=True)
    finally:
        try:
            client.close()
        except OSError:
            pass
        if upstream is not None:
            try:
                upstream.close()
            except OSError:
                pass


def serve(host: str, port: int, resolver: PublicResolver) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen(128)
        print(f"v7_public_https_proxy listening={host}:{port} dns={','.join(resolver.servers)}", flush=True)
        while True:
            client, _ = server.accept()
            threading.Thread(target=_handle, args=(client, resolver), daemon=True).start()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19109)
    parser.add_argument("--dns", action="append", default=[])
    parser.add_argument("--resolve")
    args = parser.parse_args()
    servers = args.dns or list(DEFAULT_DNS)
    resolver = PublicResolver(servers)
    if args.resolve:
        print(",".join(resolver.resolve(args.resolve)))
        return 0
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("proxy must bind loopback only")
    if not 1024 <= args.port <= 65535:
        raise SystemExit("invalid proxy port")
    serve(args.host, args.port, resolver)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
