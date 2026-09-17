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


def _driver() -> Path:
    candidates = [
        Path.cwd() / "pm_v7_native_clob_order_lane_driver",
        ROOT / "build-hft-v2" / "pm_v7_native_clob_order_lane_driver",
        ROOT / "build-Release" / "pm_v7_native_clob_order_lane_driver",
        ROOT / "build-Debug" / "pm_v7_native_clob_order_lane_driver",
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise AssertionError("native CLOB order-lane test driver was not built")

def _read_http_request(conn: ssl.SSLSocket) -> bytes:
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


def test_native_clob_order_lane_tls_ack_to_oms() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cert, key = _make_cert(tmp)
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        port = listener.getsockname()[1]
        captured: list[bytes] = []
        errors: list[BaseException] = []
        def serve() -> None:
            order_conn = cancel_conn = None
            try:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(cert, key)
                context.set_alpn_protocols(["http/1.1"])
                raw_order, _ = listener.accept()
                order_conn = context.wrap_socket(raw_order, server_side=True)
                assert order_conn.selected_alpn_protocol() == "http/1.1"
                raw_cancel, _ = listener.accept()
                cancel_conn = context.wrap_socket(raw_cancel, server_side=True)
                assert cancel_conn.selected_alpn_protocol() == "http/1.1"

                request = _read_http_request(order_conn)
                captured.append(request)
                payload = b'{"success":true,"errorMsg":"","orderID":"ex-native-1","status":"live"}'
                response = (
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(payload)}\r\n".encode()
                    + b"Connection: keep-alive\r\n\r\n" + payload
                )
                order_conn.sendall(response)
            except BaseException as exc:
                errors.append(exc)
            finally:
                for conn in (order_conn, cancel_conn):
                    if conn is not None:
                        try: conn.close()
                        except OSError: pass
                listener.close()
        thread = threading.Thread(target=serve)
        thread.start()
        result = subprocess.run(
            [str(_driver()), str(port), str(cert)],
            check=True, capture_output=True, text=True, timeout=15)
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert not errors
        assert "NATIVE_CLOB_ORDER_LANE_PASS" in result.stdout
        assert len(captured) == 1

        header, raw_body = captured[0].split(b"\r\n\r\n", 1)
        text_header = header.decode("ascii")
        assert text_header.startswith("POST /order HTTP/1.1\r\n")
        assert "POLY_ADDRESS: 0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf" in text_header
        assert "POLY_API_KEY: placeholder" in text_header
        assert "POLY_SIGNATURE:" in text_header
        body = json.loads(raw_body.decode())
        assert body["orderType"] == "FAK"
        assert body["owner"] == "placeholder"
        assert body["order"]["signatureType"] == 3
        assert body["order"]["maker"] == "0x1111111111111111111111111111111111111111"
        assert body["order"]["signer"] == "0x1111111111111111111111111111111111111111"
        assert body["order"]["side"] == "BUY"
        assert body["order"]["makerAmount"] == "2500000"
        assert body["order"]["takerAmount"] == "5000000"
        assert len(body["order"]["signature"]) == 636
