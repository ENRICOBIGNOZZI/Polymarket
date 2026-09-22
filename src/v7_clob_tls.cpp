#include "pm/v7_clob_tls.hpp"
#include "pm/socket_tuning.hpp"

#include <openssl/err.h>
#include <openssl/ssl.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <string>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

namespace pm::v7::clob {
namespace {

[[nodiscard]] std::int64_t now_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

void socket_timeout(int fd, int timeout_ms) noexcept {
    timeval tv{};
    tv.tv_sec = timeout_ms / 1000;
    tv.tv_usec = (timeout_ms % 1000) * 1000;
    (void)::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    (void)::setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
}

void tune_socket(int fd, int timeout_ms) noexcept {
    int one = 1;
    (void)::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    (void)::setsockopt(fd, SOL_SOCKET, SO_KEEPALIVE, &one, sizeof(one));
#if defined(__APPLE__) && defined(SO_NOSIGPIPE)
    (void)::setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof(one));
#endif
    socket_timeout(fd, timeout_ms);
}

[[nodiscard]] bool connect_fd(int fd, const sockaddr* address, socklen_t length,
                              int timeout_ms) noexcept {
    const int old_flags = ::fcntl(fd, F_GETFL, 0);
    if (old_flags < 0 || ::fcntl(fd, F_SETFL, old_flags | O_NONBLOCK) < 0) return false;
    const int rc = ::connect(fd, address, length);
    if (rc != 0 && errno != EINPROGRESS) {
        (void)::fcntl(fd, F_SETFL, old_flags);
        return false;
    }
    bool ok = rc == 0;
    if (!ok) {
        pollfd item{fd, POLLOUT, 0};
        const int polled = ::poll(&item, 1, timeout_ms);
        if (polled > 0 && (item.revents & POLLOUT) != 0) {
            int error = 0;
            socklen_t error_size = sizeof(error);
            ok = ::getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &error_size) == 0 && error == 0;
        }
    }
    if (::fcntl(fd, F_SETFL, old_flags) < 0) return false;
    return ok;
}

void openssl_error(char* output, std::size_t capacity, const char* prefix) noexcept {
    if (capacity == 0) return;
    const unsigned long code = ERR_get_error();
    if (code == 0) {
        std::snprintf(output, capacity, "%s", prefix);
        return;
    }
    std::array<char, 160> detail{};
    ERR_error_string_n(code, detail.data(), detail.size());
    std::snprintf(output, capacity, "%s: %s", prefix, detail.data());
}

} // namespace

PersistentTlsSession::PersistentTlsSession(std::string_view host,
                                           std::uint16_t port,
                                           int timeout_ms,
                                           int socket_busy_poll_us) noexcept
    : port_(port), timeout_ms_(timeout_ms),
      socket_busy_poll_us_(socket_busy_poll_us) {
    if (!host.empty() && host.size() < host_.size()) {
        host_size_ = host.size();
        std::memcpy(host_.data(), host.data(), host.size());
        host_[host_size_] = '\0';
    }
}

PersistentTlsSession::~PersistentTlsSession() { close(); }

void PersistentTlsSession::set_error(TlsTransportError error,
                                     std::string_view message) noexcept {
    last_error_code_ = error;
    last_error_size_ = std::min(message.size(), last_error_.size() - 1);
    std::memcpy(last_error_.data(), message.data(), last_error_size_);
    last_error_[last_error_size_] = '\0';
}

std::string_view PersistentTlsSession::last_error() const noexcept {
    return std::string_view(last_error_.data(), last_error_size_);
}

void PersistentTlsSession::abort_connection() noexcept {
    connected_ = false;
    if (ssl_ != nullptr) {
        SSL_free(reinterpret_cast<SSL*>(ssl_));
        ssl_ = nullptr;
    }
    if (ctx_ != nullptr) {
        SSL_CTX_free(reinterpret_cast<SSL_CTX*>(ctx_));
        ctx_ = nullptr;
    }
    if (fd_ >= 0) {
        (void)::shutdown(fd_, SHUT_RDWR);
        (void)::close(fd_);
        fd_ = -1;
    }
}

void PersistentTlsSession::close() noexcept {
    if (ssl_ != nullptr && connected_) {
        (void)SSL_shutdown(reinterpret_cast<SSL*>(ssl_));
    }
    abort_connection();
}

TlsConnectResult PersistentTlsSession::connect(std::string_view ca_file) noexcept {
    close();
    last_error_code_ = TlsTransportError::None;
    last_error_size_ = 0;
    TlsConnectResult out;
    if (host_size_ == 0 || port_ == 0 || timeout_ms_ <= 0) {
        out.error = TlsTransportError::InvalidArgument;
        set_error(out.error, "invalid TLS endpoint");
        return out;
    }

    char service[8]{};
    std::snprintf(service, sizeof(service), "%u", static_cast<unsigned>(port_));
    addrinfo hints{};
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_protocol = IPPROTO_TCP;
    addrinfo* addresses = nullptr;
    const auto dns_start = now_ns();
    const int resolved = ::getaddrinfo(host_.data(), service, &hints, &addresses);
    out.dns_ns = std::max<std::int64_t>(0, now_ns() - dns_start);
    if (resolved != 0 || addresses == nullptr) {
        out.error = TlsTransportError::ResolveFailed;
        set_error(out.error, "DNS resolution failed");
        if (addresses != nullptr) ::freeaddrinfo(addresses);
        return out;
    }

    const auto tcp_start = now_ns();
    for (addrinfo* current = addresses; current != nullptr; current = current->ai_next) {
        const int candidate = ::socket(current->ai_family, current->ai_socktype,
                                       current->ai_protocol);
        if (candidate < 0) continue;
        tune_socket(candidate, timeout_ms_);
        if (connect_fd(candidate, current->ai_addr,
                       static_cast<socklen_t>(current->ai_addrlen), timeout_ms_)) {
            fd_ = candidate;
            break;
        }
        (void)::close(candidate);
    }
    ::freeaddrinfo(addresses);
    out.tcp_connect_ns = std::max<std::int64_t>(0, now_ns() - tcp_start);
    if (fd_ < 0) {
        out.error = TlsTransportError::ConnectFailed;
        set_error(out.error, "TCP connect failed");
        return out;
    }
    if (const int tuning_error =
            pm::network::apply_busy_poll(fd_, socket_busy_poll_us_);
        tuning_error != 0) {
        out.error = TlsTransportError::ConnectFailed;
        set_error(out.error, "SO_BUSY_POLL setup failed");
        abort_connection();
        return out;
    }

    SSL_CTX* context = SSL_CTX_new(TLS_client_method());
    if (context == nullptr) {
        out.error = TlsTransportError::TlsContextFailed;
        set_error(out.error, "SSL_CTX_new failed");
        abort_connection();
        return out;
    }
    ctx_ = reinterpret_cast<ssl_ctx_st*>(context);
    SSL_CTX_set_verify(context, SSL_VERIFY_PEER, nullptr);
    const bool trust_ok = ca_file.empty()
        ? SSL_CTX_set_default_verify_paths(context) == 1
        : SSL_CTX_load_verify_locations(context, std::string(ca_file).c_str(), nullptr) == 1;
    if (!trust_ok) {
        out.error = TlsTransportError::TlsContextFailed;
        set_error(out.error, "CA trust setup failed");
        abort_connection();
        return out;
    }

    SSL* connection = SSL_new(context);
    if (connection == nullptr) {
        out.error = TlsTransportError::TlsContextFailed;
        set_error(out.error, "SSL_new failed");
        abort_connection();
        return out;
    }
    ssl_ = reinterpret_cast<ssl_st*>(connection);
    SSL_set_mode(connection, SSL_MODE_AUTO_RETRY);
    if (SSL_set_tlsext_host_name(connection, host_.data()) != 1
        || SSL_set1_host(connection, host_.data()) != 1
        || SSL_set_fd(connection, fd_) != 1) {
        out.error = TlsTransportError::TlsContextFailed;
        set_error(out.error, "TLS SNI/hostname/fd setup failed");
        abort_connection();
        return out;
    }
    static constexpr unsigned char kHttp11Alpn[]{8, 'h','t','t','p','/','1','.','1'};
    if (SSL_set_alpn_protos(connection, kHttp11Alpn, sizeof(kHttp11Alpn)) != 0) {
        out.error = TlsTransportError::AlpnFailed;
        set_error(out.error, "ALPN setup failed");
        abort_connection();
        return out;
    }

    const auto tls_start = now_ns();
    if (SSL_connect(connection) != 1) {
        out.tls_handshake_ns = std::max<std::int64_t>(0, now_ns() - tls_start);
        out.error = TlsTransportError::TlsHandshakeFailed;
        char message[256]{};
        openssl_error(message, sizeof(message), "TLS handshake failed");
        set_error(out.error, message);
        abort_connection();
        return out;
    }
    out.tls_handshake_ns = std::max<std::int64_t>(0, now_ns() - tls_start);
    if (SSL_get_verify_result(connection) != X509_V_OK) {
        out.error = TlsTransportError::CertificateFailed;
        set_error(out.error, "TLS certificate verification failed");
        abort_connection();
        return out;
    }
    const unsigned char* selected = nullptr;
    unsigned int selected_size = 0;
    SSL_get0_alpn_selected(connection, &selected, &selected_size);
    if (selected_size != 0) {
        if (selected_size != 8 || std::memcmp(selected, "http/1.1", 8) != 0) {
            out.error = TlsTransportError::AlpnFailed;
            set_error(out.error, "server selected non-HTTP/1.1 ALPN");
            abort_connection();
            return out;
        }
        out.alpn_http11 = 1;
    }
    connected_ = true;
    out.connected = 1;
    return out;
}

TlsWriteResult PersistentTlsSession::write_all(std::span<const char> bytes) noexcept {
    TlsWriteResult out;
    if (!connected_ || ssl_ == nullptr || bytes.empty()) {
        out.error = TlsTransportError::InvalidArgument;
        set_error(out.error, "TLS write requires connected session and bytes");
        return out;
    }
    SSL* connection = reinterpret_cast<SSL*>(ssl_);
    while (out.bytes < bytes.size()) {
        std::size_t written = 0;
        if (SSL_write_ex(connection, bytes.data() + out.bytes,
                         bytes.size() - out.bytes, &written) != 1 || written == 0) {
            out.error = TlsTransportError::WriteFailed;
            char message[256]{};
            openssl_error(message, sizeof(message), "TLS write failed");
            set_error(out.error, message);
            abort_connection();
            return out;
        }
        out.bytes += written;
    }
    out.completed_monotonic_ns = now_ns();
    out.ok = 1;
    return out;
}

TlsReadResult PersistentTlsSession::read_some(std::span<char> output) noexcept {
    TlsReadResult out;
    if (!connected_ || ssl_ == nullptr || output.empty()) {
        out.error = TlsTransportError::InvalidArgument;
        set_error(out.error, "TLS read requires connected session and buffer");
        return out;
    }
    SSL* connection = reinterpret_cast<SSL*>(ssl_);
    std::size_t received = 0;
    const int rc = SSL_read_ex(connection, output.data(), output.size(), &received);
    out.completed_monotonic_ns = now_ns();
    out.incoming_cpu = pm::network::incoming_cpu(fd_);
    out.incoming_napi_id = pm::network::incoming_napi_id(fd_);
    if (rc == 1 && received > 0) {
        out.bytes = received;
        out.ok = 1;
        return out;
    }
    const int ssl_error = SSL_get_error(connection, rc);
    if (ssl_error == SSL_ERROR_ZERO_RETURN) {
        out.peer_closed = 1;
        abort_connection();
        return out;
    }
    out.error = TlsTransportError::ReadFailed;
    char message[256]{};
    openssl_error(message, sizeof(message), "TLS read failed");
    set_error(out.error, message);
    abort_connection();
    return out;
}

} // namespace pm::v7::clob
