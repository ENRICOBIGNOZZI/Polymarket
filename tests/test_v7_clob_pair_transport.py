from __future__ import annotations

import json
import socket
import ssl
import subprocess
import tempfile
import threading
import time
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


def _driver() -> Path:
    candidates = [
        Path.cwd() / "pm_v7_clob_pair_transport_driver",
        ROOT / "build-Release" / "pm_v7_clob_pair_transport_driver",
        ROOT / "build-Debug" / "pm_v7_clob_pair_transport_driver",
    ]
    candidates.extend(sorted(
        ROOT.glob("build*/pm_v7_clob_pair_transport_driver")
    ))
    for path in candidates:
        if path.is_file():
            return path
    raise AssertionError("paired CLOB transport test driver was not built")


def _read_request(conn: ssl.SSLSocket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        assert chunk
        data += chunk
    header, body = data.split(b"\r\n\r\n", 1)
    content_length = 0
    for line in header.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            content_length = int(line.split(b":", 1)[1].strip())
    while len(body) < content_length:
        chunk = conn.recv(4096)
        assert chunk
        body += chunk
    return header + b"\r\n\r\n" + body[:content_length]


def test_paired_transport_uses_independent_persistent_order_lanes() -> None:
    driver = _driver()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cert, key = _make_cert(tmp)
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(3)
        listener.settimeout(10)
        port = listener.getsockname()[1]
        captured: list[bytes] = []
        errors: list[BaseException] = []

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

                lock = threading.Lock()

                def handle(index: int, delay: float) -> None:
                    req = _read_request(conns[index])
                    with lock:
                        captured.append(req)
                    time.sleep(delay)
                    payload = b'{"success":true}'
                    response = (
                        b"HTTP/1.1 200 OK\r\n"
                        + f"Content-Length: {len(payload)}\r\n".encode()
                        + b"Connection: keep-alive\r\n\r\n"
                        + payload
                    )
                    conns[index].sendall(response)

                threads = [
                    threading.Thread(target=handle, args=(0, 0.03)),
                    threading.Thread(target=handle, args=(1, 0.01)),
                    threading.Thread(target=handle, args=(2, 0.015)),
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
            [str(driver), str(port), str(cert)],
            check=True, capture_output=True, text=True, timeout=15)
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert not errors

        report = json.loads(result.stdout)
        assert report["paper_only"] is True
        assert report["authenticated_execution"] is False
        assert report["real_order_submission"] is False
        assert report["parallel_wire_skew_ns"] >= 0
        assert report["parallel_ack_skew_ns"] >= 0
        assert report["parallel_yes_write_to_ack_ns"] > 0
        assert report["parallel_no_write_to_ack_ns"] > 0
        assert report["batch_write_to_ack_ns"] > 0

        assert len(captured) == 3
        starts = sorted(
            row.split(b"\r\n", 1)[0] for row in captured
        )
        assert starts.count(b"POST /order HTTP/1.1") == 2
        assert starts.count(b"POST /orders HTTP/1.1") == 1
