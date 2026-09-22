from __future__ import annotations

import json
import socket
import ssl
import subprocess
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _make_cert(tmp: Path) -> tuple[Path, Path]:
    cert, key = tmp / "cert.pem", tmp / "key.pem"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(cert), "-days", "1",
        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return cert, key


def _read_request(conn: ssl.SSLSocket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            raise EOFError("connection closed before headers")
        data += chunk
    header, body = data.split(b"\r\n\r\n", 1)
    length = 0
    for line in header.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    while len(body) < length:
        chunk = conn.recv(4096)
        if not chunk:
            raise EOFError("connection closed before body")
        body += chunk
    return header.split(b"\r\n", 1)[0]


def _response() -> bytes:
    payload = b'{"success":true}'
    return (
        b"HTTP/1.1 200 OK\r\n"
        + f"Content-Length: {len(payload)}\r\n".encode()
        + b"Connection: keep-alive\r\n\r\n"
        + payload
    )


def _find_driver() -> Path:
    candidates = [
        Path.cwd() / "pm_v7_clob_pair_transport_bench_driver",
        ROOT / "build-Release" / "pm_v7_clob_pair_transport_bench_driver",
        ROOT / "build-Debug" / "pm_v7_clob_pair_transport_bench_driver",
    ]
    candidates.extend(sorted(
        ROOT.glob("build*/pm_v7_clob_pair_transport_bench_driver")
    ))
    for path in candidates:
        if path.is_file():
            return path
    raise AssertionError("paired transport benchmark driver was not built")


def test_persistent_pair_vs_batch_transport_benchmark() -> None:
    driver = _find_driver()
    samples = 400
    per_lane = samples + 32

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cert, key = _make_cert(tmp)
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(3)
        listener.settimeout(10)
        port = listener.getsockname()[1]
        errors: list[BaseException] = []
        counts = [0, 0, 0]

        def server() -> None:
            conns: list[ssl.SSLSocket] = []
            try:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(cert, key)
                context.set_alpn_protocols(["http/1.1"])
                for _ in range(3):
                    raw, _ = listener.accept()
                    raw.settimeout(10)
                    conn = context.wrap_socket(raw, server_side=True)
                    assert conn.selected_alpn_protocol() == "http/1.1"
                    conns.append(conn)

                def handle(index: int, expected_start: bytes) -> None:
                    for _ in range(per_lane):
                        start = _read_request(conns[index])
                        assert start == expected_start
                        conns[index].sendall(_response())
                        counts[index] += 1

                threads = [
                    threading.Thread(
                        target=handle,
                        args=(0, b"POST /order HTTP/1.1")),
                    threading.Thread(
                        target=handle,
                        args=(1, b"POST /order HTTP/1.1")),
                    threading.Thread(
                        target=handle,
                        args=(2, b"POST /orders HTTP/1.1")),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
            except BaseException as exc:
                errors.append(exc)
            finally:
                for conn in conns:
                    try:
                        conn.close()
                    except OSError:
                        pass
                listener.close()

        thread = threading.Thread(target=server)
        thread.start()
        result = subprocess.run(
            [str(driver), str(port), str(cert), "--samples", str(samples)],
            check=True, capture_output=True, text=True, timeout=30)
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert not errors
        assert counts == [per_lane, per_lane, per_lane]

        report = json.loads(result.stdout)
        assert report["paper_only"] is True
        assert report["authenticated_execution"] is False
        assert report["real_order_submission"] is False
        assert report["samples"] == samples
        assert report["scope"] == "LOCAL_TLS_TRANSPORT_OVERHEAD_NOT_VENUE_EXECUTION"
        latency = report["latency_ns"]
        for key in (
            "parallel_pair_write_to_both_ack",
            "parallel_wire_skew",
            "parallel_ack_skew",
            "batch_write_to_ack",
        ):
            row = latency[key]
            assert 0 <= row["p50"] <= row["p95"] <= row["p99"] <= row["p999"] <= row["max"]
