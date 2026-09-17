from __future__ import annotations

import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROGRAM = r'''
#include "pm/v7_clob_tls.hpp"
#include <array>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string_view>

using pm::v7::clob::PersistentTlsSession;

bool read_until(PersistentTlsSession& session, std::string_view needle) {
    std::array<char, 1024> buffer{};
    std::array<char, 4096> all{};
    std::size_t used = 0;
    while (used < all.size()) {
        auto result = session.read_some(std::span<char>(buffer.data(), buffer.size()));
        if (!result.ok || result.bytes == 0 || used + result.bytes > all.size()) return false;
        std::memcpy(all.data() + used, buffer.data(), result.bytes);
        used += result.bytes;
        if (std::string_view(all.data(), used).find(needle) != std::string_view::npos) return true;
    }
    return false;
}

int main(int argc, char** argv) {
    if (argc != 3) return 2;
    const int port = std::atoi(argv[1]);
    PersistentTlsSession session("localhost", static_cast<unsigned short>(port), 2000);
    auto connected = session.connect(argv[2]);
    if (!connected.connected || !connected.alpn_http11) {
        std::fprintf(stderr, "connect failed: %.*s\n", (int)session.last_error().size(), session.last_error().data());
        return 3;
    }
    constexpr std::string_view r1 = "GET /time HTTP/1.1\r\nHost: localhost\r\nConnection: keep-alive\r\n\r\n";
    auto w1 = session.write_all(std::span<const char>(r1.data(), r1.size()));
    if (!w1.ok || !read_until(session, "one")) return 4;
    constexpr std::string_view r2 = "GET /time HTTP/1.1\r\nHost: localhost\r\nConnection: keep-alive\r\n\r\n";
    auto w2 = session.write_all(std::span<const char>(r2.data(), r2.size()));
    if (!w2.ok || !read_until(session, "two")) return 5;
    if (!session.connected()) return 6;
    std::printf("dns_ns=%lld tcp_ns=%lld tls_ns=%lld write1=%zu write2=%zu\n",
        (long long)connected.dns_ns, (long long)connected.tcp_connect_ns,
        (long long)connected.tls_handshake_ns, w1.bytes, w2.bytes);
    return 0;
}
'''


def _openssl_flags() -> tuple[list[str], list[str]]:
    prefix = subprocess.check_output(["brew", "--prefix", "openssl@3"], text=True).strip()
    return [f"-I{prefix}/include"], [f"-L{prefix}/lib", "-lssl", "-lcrypto"]


def _make_cert(tmp: Path) -> tuple[Path, Path]:
    cert = tmp / "cert.pem"
    key = tmp / "key.pem"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(cert), "-days", "1",
        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return cert, key


def test_persistent_tls_session_reuses_one_connection() -> None:
    compiler = shutil.which("c++")
    assert compiler
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        cert, key = _make_cert(tmp)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        accepted = []
        errors: list[BaseException] = []

        def serve() -> None:
            try:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(cert, key)
                context.set_alpn_protocols(["http/1.1"])
                raw, _ = listener.accept()
                accepted.append(1)
                with context.wrap_socket(raw, server_side=True) as conn:
                    assert conn.selected_alpn_protocol() == "http/1.1"
                    for body in (b"one", b"two"):
                        request = b""
                        while b"\r\n\r\n" not in request:
                            request += conn.recv(4096)
                        assert request.startswith(b"GET /time HTTP/1.1\r\n")
                        response = (
                            b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                            + f"Content-Length: {len(body)}\r\n".encode()
                            + b"Connection: keep-alive\r\n\r\n" + body
                        )
                        conn.sendall(response)
            except BaseException as exc:
                errors.append(exc)
            finally:
                listener.close()

        thread = threading.Thread(target=serve)
        thread.start()
        main = tmp / "main.cpp"
        binary = tmp / "tls-test"
        main.write_text(PROGRAM)
        inc, libs = _openssl_flags()
        subprocess.run([
            compiler, "-std=c++20", "-O2", "-Wall", "-Wextra", "-Wpedantic",
            f"-I{ROOT / 'include'}", *inc, str(ROOT / "src/v7_clob_tls.cpp"),
            str(main), "-o", str(binary), *libs,
        ], check=True, capture_output=True, text=True)
        result = subprocess.run([str(binary), str(port), str(cert)], check=True,
                                capture_output=True, text=True, timeout=10)
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert not errors
        assert len(accepted) == 1
        assert "write1=" in result.stdout and "write2=" in result.stdout
