from __future__ import annotations

import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROGRAM = r'''#include "pm/v7_clob_tls_write_plane.hpp"
#include <openssl/ssl.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <unistd.h>
#include <array>
#include <cstdlib>

int main(int argc, char** argv) {
    using namespace pm::v7::clob_transport;
    if (argc != 2 || configure_order_socket_low_latency(-1) || configure_order_tls_low_latency(nullptr)) return 10;
    const int port = std::atoi(argv[1]);
    const int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0 || !configure_order_socket_low_latency(fd)) return 11;
    int nodelay = 0; socklen_t option_size = sizeof(nodelay);
    if (::getsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &nodelay, &option_size) != 0 || nodelay == 0) return 12;
    sockaddr_in address{}; address.sin_family = AF_INET; address.sin_port = htons(port);
    if (::inet_pton(AF_INET, "127.0.0.1", &address.sin_addr) != 1) return 13;
    if (::connect(fd, reinterpret_cast<sockaddr*>(&address), sizeof(address)) != 0) return 14;
    SSL_CTX* context = ::SSL_CTX_new(TLS_client_method());
    if (!context) return 15;
    ::SSL_CTX_set_verify(context, SSL_VERIFY_NONE, nullptr);
    SSL* ssl = ::SSL_new(context);
    if (!ssl) return 16;
    if (!configure_order_tls_low_latency(ssl)) return 17;
    ::SSL_set_fd(ssl, fd);
    if (::SSL_connect(ssl) != 1) return 22;

    TlsOrderWritePlane plane(ssl);
    const std::array<char, 5> first{'h','e','l','l','o'};
    const std::array<char, 5> second{'w','o','r','l','d'};
    const auto a = plane.send_frame(first);
    if (a.disposition != TlsWriteDisposition::SentAwaitingResponse || a.bytes_accepted != first.size()) return 18;
    const auto blocked = plane.send_frame(second);
    if (blocked.disposition != TlsWriteDisposition::NotSent || blocked.bytes_accepted != 0) return 19;
    if (!plane.mark_response_drained()) return 20;
    const auto b = plane.send_frame(second);
    if (b.disposition != TlsWriteDisposition::SentAwaitingResponse || b.bytes_accepted != second.size()) return 21;

    ::SSL_free(ssl); ::SSL_CTX_free(context); ::close(fd);
    return 0;
}
'''


def _openssl_flags() -> tuple[list[str], list[str]]:
    pkg = shutil.which("pkg-config")
    if pkg:
        cflags = subprocess.run([pkg, "--cflags", "openssl"], check=True, capture_output=True, text=True).stdout.split()
        libs = subprocess.run([pkg, "--libs", "openssl"], check=True, capture_output=True, text=True).stdout.split()
        return cflags, libs
    return [], ["-lssl", "-lcrypto"]


def test_tls_write_plane_single_owner_and_nodelay() -> None:
    compiler = shutil.which("c++")
    openssl = shutil.which("openssl")
    assert compiler and openssl
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        cert = tmp / "cert.pem"
        key = tmp / "key.pem"
        subprocess.run([
            openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-subj", "/CN=localhost", "-keyout", str(key), "-out", str(cert), "-days", "1",
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        received: list[bytes] = []
        server_error: list[BaseException] = []

        def serve() -> None:
            try:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(cert, key)
                raw, _ = listener.accept()
                with context.wrap_socket(raw, server_side=True) as connection:
                    data = b""
                    while len(data) < 10:
                        chunk = connection.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    received.append(data)
            except BaseException as exc:
                server_error.append(exc)
            finally:
                listener.close()

        thread = threading.Thread(target=serve)
        thread.start()
        source = tmp / "main.cpp"
        binary = tmp / "tls-write-plane-test"
        source.write_text(PROGRAM)
        cflags, libs = _openssl_flags()
        subprocess.run([
            compiler, "-std=c++20", "-O2", "-Wall", "-Wextra", "-Wpedantic",
            *cflags, f"-I{ROOT / 'include'}", str(ROOT / "src/v7_clob_tls_write_plane.cpp"),
            str(source), "-o", str(binary), *libs,
        ], check=True, capture_output=True, text=True)
        subprocess.run([str(binary), str(port)], check=True, timeout=10)
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert not server_error
        assert received == [b"helloworld"]


if __name__ == "__main__":
    test_tls_write_plane_single_owner_and_nodelay()
