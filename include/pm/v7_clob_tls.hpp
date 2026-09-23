#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <span>
#include <string_view>

struct ssl_ctx_st;
struct ssl_st;

namespace pm::v7::clob {

enum class TlsTransportError : std::uint8_t {
    None = 0,
    InvalidArgument,
    ResolveFailed,
    ConnectFailed,
    TlsContextFailed,
    TlsHandshakeFailed,
    CertificateFailed,
    AlpnFailed,
    WriteFailed,
    ReadFailed,
};

struct TlsConnectResult {
    std::int64_t dns_ns = 0;
    std::int64_t tcp_connect_ns = 0;
    std::int64_t tls_handshake_ns = 0;
    TlsTransportError error = TlsTransportError::None;
    std::uint8_t connected = 0;
    std::uint8_t alpn_http11 = 0;
};

struct TlsWriteResult {
    std::size_t bytes = 0;
    std::int64_t completed_monotonic_ns = 0;
    TlsTransportError error = TlsTransportError::None;
    std::uint8_t ok = 0;
};

struct TlsReadResult {
    std::size_t bytes = 0;
    std::int64_t completed_monotonic_ns = 0;
    int incoming_cpu = -1;
    int incoming_napi_id = -1;
    TlsTransportError error = TlsTransportError::None;
    std::uint8_t ok = 0;
    std::uint8_t peer_closed = 0;
};

class PersistentTlsSession final {
public:
    explicit PersistentTlsSession(std::string_view host,
                                  std::uint16_t port = 443,
                                  int timeout_ms = 2'000,
                                  int socket_busy_poll_us = 0,
                                  bool capture_rx_metadata = false) noexcept;
    ~PersistentTlsSession();

    PersistentTlsSession(const PersistentTlsSession&) = delete;
    PersistentTlsSession& operator=(const PersistentTlsSession&) = delete;

    [[nodiscard]] TlsConnectResult connect(std::string_view ca_file = {}) noexcept;
    void close() noexcept;
    [[nodiscard]] bool connected() const noexcept { return connected_; }

    [[nodiscard]] TlsWriteResult write_all(std::span<const char> bytes) noexcept;
    [[nodiscard]] TlsReadResult read_some(std::span<char> output) noexcept;
    [[nodiscard]] std::string_view last_error() const noexcept;

private:
    void abort_connection() noexcept;
    void set_error(TlsTransportError error, std::string_view message) noexcept;

    std::array<char, 256> host_{};
    std::size_t host_size_ = 0;
    std::uint16_t port_ = 443;
    int timeout_ms_ = 2'000;
    int socket_busy_poll_us_ = 0;
    bool capture_rx_metadata_ = false;
    int fd_ = -1;
    ssl_ctx_st* ctx_ = nullptr;
    ssl_st* ssl_ = nullptr;
    bool connected_ = false;
    TlsTransportError last_error_code_ = TlsTransportError::None;
    std::array<char, 256> last_error_{};
    std::size_t last_error_size_ = 0;
};

} // namespace pm::v7::clob
