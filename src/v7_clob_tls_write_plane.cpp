#include "pm/v7_clob_tls_write_plane.hpp"
#include <openssl/ssl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <climits>
namespace pm::v7::clob_transport {
bool configure_order_socket_low_latency(int fd) noexcept {
    if (fd < 0) return false;
    const int enabled = 1;
    return ::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY,
                        &enabled, sizeof(enabled)) == 0;
}

bool configure_order_tls_low_latency(SSL* ssl) noexcept {
    if (ssl == nullptr) return false;
    constexpr const char* kTls13Order =
        "TLS_AES_128_GCM_SHA256:"
        "TLS_AES_256_GCM_SHA384:"
        "TLS_CHACHA20_POLY1305_SHA256";
    return ::SSL_set_ciphersuites(ssl, kTls13Order) == 1;
}
TlsWriteResult TlsOrderWritePlane::send_frame(std::span<const char> frame) noexcept {
    TlsWriteResult result{};
    if (!ready() || frame.empty() || frame.size() > static_cast<std::size_t>(INT_MAX)) return result;
    std::size_t written = 0;
    const int status = ::SSL_write_ex(ssl_, frame.data(), frame.size(), &written);
    result.bytes_accepted = written;
    if (status == 1 && written == frame.size()) {
        response_outstanding_ = true;
        result.disposition = TlsWriteDisposition::SentAwaitingResponse;
        return result;
    }
    result.ssl_error = ::SSL_get_error(ssl_, status);
    result.disposition = TlsWriteDisposition::Ambiguous;
    poisoned_ = true;
    return result;
}
bool TlsOrderWritePlane::mark_response_drained() noexcept {
    if (poisoned_ || !response_outstanding_) return false;
    response_outstanding_ = false;
    return true;
}
} // namespace pm::v7::clob_transport
