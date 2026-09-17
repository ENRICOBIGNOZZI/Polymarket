#include "pm/v7_user_ws_feed.hpp"

#include <boost/asio/connect.hpp>
#include <boost/asio/ip/tcp.hpp>
#include <boost/asio/ssl/context.hpp>
#include <boost/asio/ssl/stream.hpp>
#include <boost/asio/steady_timer.hpp>
#if __has_include(<boost/asio/ssl/host_name_verification.hpp>)
#include <boost/asio/ssl/host_name_verification.hpp>
#define PM_USER_WS_HOST_VERIFY 1
#else
#include <boost/asio/ssl/rfc2818_verification.hpp>
#define PM_USER_WS_HOST_VERIFY 0
#endif
#include <boost/beast/core.hpp>
#include <boost/beast/ssl.hpp>
#include <boost/beast/websocket.hpp>
#include <openssl/err.h>
#include <openssl/ssl.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <functional>
#include <stdexcept>
#include <thread>
#include <utility>

namespace pm::v7::user_ws {
namespace {
namespace asio = boost::asio;
namespace beast = boost::beast;
namespace websocket = beast::websocket;
namespace ssl = asio::ssl;
using tcp = asio::ip::tcp;

constexpr std::string_view kHost = "ws-subscriptions-clob.polymarket.com";
constexpr std::string_view kPort = "443";
constexpr std::string_view kTarget = "/ws/user";

[[nodiscard]] std::int64_t monotonic_now_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

[[nodiscard]] std::string openssl_error() {
    const auto code = ::ERR_get_error();
    if (code == 0) return "unknown TLS error";
    char buffer[256]{};
    ::ERR_error_string_n(code, buffer, sizeof(buffer));
    return buffer;
}

} // namespace

struct AuthenticatedFeed::Impl {
    std::string api_key;
    std::string secret;
    std::string passphrase;
    std::vector<std::string> condition_ids;
    std::string subscription;
    EventHandler on_event;
    ErrorHandler on_error;
    ReconnectHandler on_reconnect;
    std::thread worker;
    std::atomic<bool> stop_requested{false};
    std::atomic<bool> started{false};
    std::atomic<bool> connected{false};
    std::atomic<std::uint64_t> frames{0};
    std::atomic<std::uint64_t> decoded_events{0};
    std::atomic<std::uint64_t> invalid_frames{0};
    std::atomic<std::uint64_t> reconnects{0};
    std::atomic<std::uint64_t> transport_errors{0};

    Impl(CredentialsView credentials,
         std::vector<std::string> markets,
         EventHandler event_handler,
         ErrorHandler error_handler,
         ReconnectHandler reconnect_handler)
        : api_key(credentials.api_key),
          secret(credentials.secret),
          passphrase(credentials.passphrase),
          condition_ids(std::move(markets)),
          on_event(std::move(event_handler)),
          on_error(std::move(error_handler)),
          on_reconnect(std::move(reconnect_handler)) {
        if (api_key.empty() || secret.empty() || passphrase.empty()) {
            throw std::invalid_argument("Polymarket user websocket credentials are required");
        }
        if (!on_event) throw std::invalid_argument("user websocket event handler is required");
        condition_ids.erase(
            std::remove_if(condition_ids.begin(), condition_ids.end(),
                           [](const std::string& value) { return value.empty(); }),
            condition_ids.end());
        std::sort(condition_ids.begin(), condition_ids.end());
        condition_ids.erase(std::unique(condition_ids.begin(), condition_ids.end()),
                            condition_ids.end());
        std::vector<std::string_view> views;
        views.reserve(condition_ids.size());
        for (const auto& value : condition_ids) views.emplace_back(value);
        subscription = subscription_json(
            CredentialsView{api_key, secret, passphrase}, views);
    }

    ~Impl() {
        // Best-effort in-process credential lifetime reduction. The authoritative
        // credential owner remains outside this component.
        std::fill(secret.begin(), secret.end(), '\0');
        std::fill(passphrase.begin(), passphrase.end(), '\0');
        std::fill(subscription.begin(), subscription.end(), '\0');
    }

    void report(std::string_view message) {
        transport_errors.fetch_add(1, std::memory_order_relaxed);
        if (on_error) on_error(message);
    }

    void run() {
        int backoff_seconds = 1;
        bool ever_connected = false;
        beast::flat_buffer buffer;
        buffer.reserve(256U * 1024U);

        while (!stop_requested.load(std::memory_order_relaxed)) {
            bool marked_connected = false;
            try {
                asio::io_context io;
                ssl::context tls(ssl::context::tls_client);
                tls.set_default_verify_paths();
                tls.set_verify_mode(ssl::verify_peer);

                websocket::stream<beast::ssl_stream<beast::tcp_stream>> ws(io, tls);
                const std::string host(kHost);
                if (!::SSL_set_tlsext_host_name(ws.next_layer().native_handle(),
                                                host.c_str())) {
                    throw std::runtime_error("user websocket SNI failed: " + openssl_error());
                }
#if PM_USER_WS_HOST_VERIFY
                ws.next_layer().set_verify_callback(ssl::host_name_verification(host));
#else
                ws.next_layer().set_verify_callback(ssl::rfc2818_verification(host));
#endif

                tcp::resolver resolver(io);
                const auto resolved = resolver.resolve(host, std::string(kPort));
                beast::get_lowest_layer(ws).expires_after(std::chrono::seconds(10));
                beast::get_lowest_layer(ws).connect(resolved);
                beast::get_lowest_layer(ws).socket().set_option(tcp::no_delay(true));
                ws.next_layer().handshake(ssl::stream_base::client);
                beast::get_lowest_layer(ws).expires_never();
                ws.set_option(websocket::stream_base::timeout{
                    std::chrono::seconds(10), std::chrono::seconds(60), false});
                ws.set_option(websocket::stream_base::decorator(
                    [](websocket::request_type& request) {
                        request.set(beast::http::field::user_agent,
                                    "polymarket-v7-user-feed/1.0");
                    }));
                ws.handshake(host, std::string(kTarget));
                ws.text(true);
                ws.write(asio::buffer(subscription));

                connected.store(true, std::memory_order_release);
                marked_connected = true;
                backoff_seconds = 1;
                if (ever_connected) {
                    reconnects.fetch_add(1, std::memory_order_relaxed);
                    // This callback is the cold reconciliation boundary. It runs
                    // before the first post-reconnect frame is consumed.
                    if (on_reconnect) on_reconnect();
                }
                ever_connected = true;

                asio::steady_timer heartbeat(io);
                asio::steady_timer stop_poll(io);
                bool terminal = false;
                std::string transport_error;

                const auto finish = [&](beast::error_code error) {
                    if (terminal) return;
                    terminal = true;
                    if (error && error != websocket::error::closed
                        && !stop_requested.load(std::memory_order_relaxed)) {
                        transport_error = error.message();
                    }
                    beast::error_code ignored;
                    heartbeat.cancel();
                    stop_poll.cancel();
                    beast::get_lowest_layer(ws).socket().cancel(ignored);
                };

                std::function<void()> begin_read;
                std::function<void()> schedule_ping;
                std::function<void()> schedule_stop_poll;

                begin_read = [&] {
                    if (terminal || stop_requested.load(std::memory_order_relaxed)) return;
                    buffer.consume(buffer.size());
                    ws.async_read(buffer, [&](beast::error_code error, std::size_t) {
                        const auto receive_ns = monotonic_now_ns();
                        if (error) {
                            finish(error);
                            return;
                        }
                        const auto front = beast::buffers_front(buffer.data());
                        const std::string_view message{
                            static_cast<const char*>(front.data()), front.size()};
                        if (!message.empty()) {
                            frames.fetch_add(1, std::memory_order_relaxed);
                            const auto parsed = decode(message, receive_ns);
                            if (parsed.invalid != 0) {
                                invalid_frames.fetch_add(1, std::memory_order_relaxed);
                            } else if (parsed.recognized != 0
                                       && parsed.event.kind != EventKind::Pong) {
                                decoded_events.fetch_add(1, std::memory_order_relaxed);
                                on_event(parsed.event);
                            }
                        }
                        begin_read();
                    });
                };

                schedule_ping = [&] {
                    if (terminal || stop_requested.load(std::memory_order_relaxed)) return;
                    heartbeat.expires_after(
                        std::chrono::milliseconds(kHeartbeatIntervalMs));
                    heartbeat.async_wait([&](beast::error_code error) {
                        if (error || terminal
                            || stop_requested.load(std::memory_order_relaxed)) return;
                        ws.text(true);
                        ws.async_write(asio::buffer(kHeartbeatRequest),
                                       [&](beast::error_code write_error, std::size_t) {
                            if (write_error) {
                                finish(write_error);
                                return;
                            }
                            schedule_ping();
                        });
                    });
                };

                schedule_stop_poll = [&] {
                    if (terminal) return;
                    stop_poll.expires_after(std::chrono::milliseconds(50));
                    stop_poll.async_wait([&](beast::error_code error) {
                        if (error || terminal) return;
                        if (stop_requested.load(std::memory_order_relaxed)) {
                            terminal = true;
                            beast::error_code ignored;
                            heartbeat.cancel();
                            auto& socket = beast::get_lowest_layer(ws).socket();
                            socket.cancel(ignored);
                            socket.shutdown(tcp::socket::shutdown_both, ignored);
                            socket.close(ignored);
                            io.stop();
                            return;
                        }
                        schedule_stop_poll();
                    });
                };

                begin_read();
                schedule_ping();
                schedule_stop_poll();
                io.run();
                if (!stop_requested.load(std::memory_order_relaxed)
                    && !transport_error.empty()) {
                    throw std::runtime_error(transport_error);
                }
            } catch (const std::exception& error) {
                if (!stop_requested.load(std::memory_order_relaxed)) report(error.what());
            }

            if (marked_connected) connected.store(false, std::memory_order_release);
            if (!stop_requested.load(std::memory_order_relaxed)) {
                for (int tenth = 0; tenth < backoff_seconds * 10
                     && !stop_requested.load(std::memory_order_relaxed); ++tenth) {
                    std::this_thread::sleep_for(std::chrono::milliseconds(100));
                }
                backoff_seconds = std::min(30, backoff_seconds * 2);
            }
        }
    }

    void start() {
        bool expected = false;
        if (!started.compare_exchange_strong(expected, true)) return;
        stop_requested.store(false, std::memory_order_relaxed);
        worker = std::thread([this] { run(); });
    }

    void stop() {
        if (!started.exchange(false)) return;
        stop_requested.store(true, std::memory_order_relaxed);
        if (worker.joinable()) worker.join();
        connected.store(false, std::memory_order_release);
    }
};

AuthenticatedFeed::AuthenticatedFeed(CredentialsView credentials,
                                     std::vector<std::string> condition_ids,
                                     EventHandler on_event,
                                     ErrorHandler on_error,
                                     ReconnectHandler on_reconnect)
    : impl_(std::make_unique<Impl>(credentials, std::move(condition_ids),
                                   std::move(on_event), std::move(on_error),
                                   std::move(on_reconnect))) {}

AuthenticatedFeed::~AuthenticatedFeed() { stop(); }
void AuthenticatedFeed::start() { impl_->start(); }
void AuthenticatedFeed::stop() { impl_->stop(); }

FeedSnapshot AuthenticatedFeed::snapshot() const noexcept {
    return FeedSnapshot{
        static_cast<std::uint8_t>(impl_->connected.load(std::memory_order_acquire)),
        impl_->frames.load(std::memory_order_relaxed),
        impl_->decoded_events.load(std::memory_order_relaxed),
        impl_->invalid_frames.load(std::memory_order_relaxed),
        impl_->reconnects.load(std::memory_order_relaxed),
        impl_->transport_errors.load(std::memory_order_relaxed),
    };
}

} // namespace pm::v7::user_ws
