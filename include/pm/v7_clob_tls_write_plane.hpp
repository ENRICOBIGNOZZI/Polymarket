#pragma once
#include <cstddef>
#include <cstdint>
#include <span>
struct ssl_st;
using SSL = ssl_st;
namespace pm::v7::clob_transport {
enum class TlsWriteDisposition : std::uint8_t {
    NotSent = 0, SentAwaitingResponse = 1, Ambiguous = 2,
};
struct TlsWriteResult {
    TlsWriteDisposition disposition = TlsWriteDisposition::NotSent;
    std::size_t bytes_accepted = 0;
    int ssl_error = 0;
};
[[nodiscard]] bool configure_order_socket_low_latency(int fd) noexcept;

// Cold-path, pre-handshake preference order for TLS 1.3. Keeps all three
// standard suites as fallbacks while preferring AES-128-GCM on AES-accelerated
// London hosts. TLS <=1.2 configuration is left untouched.
[[nodiscard]] bool configure_order_tls_low_latency(SSL* ssl) noexcept;
class TlsOrderWritePlane final {
public:
    explicit TlsOrderWritePlane(SSL* ssl) noexcept : ssl_(ssl) {}
    [[nodiscard]] bool ready() const noexcept {
        return ssl_ != nullptr && !response_outstanding_ && !poisoned_;
    }
    [[nodiscard]] TlsWriteResult send_frame(std::span<const char> frame) noexcept;
    [[nodiscard]] bool mark_response_drained() noexcept;
    void poison() noexcept { poisoned_ = true; }
private:
    SSL* ssl_ = nullptr;
    bool response_outstanding_ = false;
    bool poisoned_ = false;
};
} // namespace pm::v7::clob_transport
