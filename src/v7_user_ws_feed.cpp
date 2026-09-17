#include "pm/v7_user_ws.hpp"

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
#include <boost/json.hpp>
#include <openssl/err.h>
#include <openssl/ssl.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <functional>
#include <stdexcept>
#include <thread>
#include <utility>

namespace pm::v7 {
namespace {
namespace asio = boost::asio;
namespace beast = boost::beast;
namespace websocket = beast::websocket;
namespace ssl = asio::ssl;
namespace json = boost::json;
using tcp = asio::ip::tcp;

constexpr std::string_view kUserHost = "ws-subscriptions-clob.polymarket.com";
constexpr std::string_view kUserPort = "443";
constexpr std::string_view kUserTarget = "/ws/user";

[[nodiscard]] pm::fast::FeedReceiveStamp receive_stamp() noexcept {
    pm::fast::FeedReceiveStamp stamp;
    stamp.monotonic_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
                             std::chrono::steady_clock::now().time_since_epoch())
                             .count();
    stamp.wall_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                        std::chrono::system_clock::now().time_since_epoch())
                        .count();
    return stamp;
}

[[nodiscard]] std::string openssl_error() {
    const auto code = ::ERR_get_error();
    if (code == 0) return "unknown TLS error";
    char buffer[256]{};
    ::ERR_error_string_n(code, buffer, sizeof(buffer));
    return buffer;
}

[[nodiscard]] std::string subscription(const UserWsCredentials& credentials,
                                       const std::vector<std::string>& markets) {
    json::object auth{
        {"apiKey", credentials.api_key},
        {"secret", credentials.secret},
        {"passphrase", credentials.passphrase},
    };
    json::object request{
        {"auth", std::move(auth)},
        {"type", "user"},
    };
    if (!markets.empty()) {
        json::array selected;
        selected.reserve(markets.size());
        for (const auto& market : markets) selected.emplace_back(market);
        request["markets"] = std::move(selected);
    }
    return json::serialize(request);
}

} // namespace

struct UserWebSocketFeed::Impl {
    UserWsCredentials credentials;
    std::vector<std::string> markets;
    std::string subscription_json;
    MessageHandler on_message;
    ErrorHandler on_error;
    std::thread worker;
    std::atomic<bool> stop_requested{false};
    std::atomic<bool> started{false};
    std::atomic<bool> connected{false};
    std::atomic<std::uint64_t> messages{0};
    std::atomic<std::uint64_t> reconnects{0};
    std::atomic<std::uint64_t> errors{0};

    Impl(UserWsCredentials input_credentials,
         std::vector<std::string> input_markets,
         MessageHandler message_handler,
         ErrorHandler error_handler)
        : credentials(std::move(input_credentials)),
          markets(std::move(input_markets)),
          on_message(std::move(message_handler)),
          on_error(std::move(error_handler)) {
        if (credentials.api_key.empty() || credentials.secret.empty()
            || credentials.passphrase.empty()) {
            throw std::invalid_argument("Polymarket user WebSocket credentials are required");
        }
        if (!on_message) throw std::invalid_argument("user WebSocket handler is required");
        markets.erase(std::remove_if(markets.begin(), markets.end(),
                                     [](const std::string& value) { return value.empty(); }),
                      markets.end());
        std::sort(markets.begin(), markets.end());
        markets.erase(std::unique(markets.begin(), markets.end()), markets.end());
        subscription_json = subscription(credentials, markets);
    }

    void report(std::string_view message) {
        errors.fetch_add(1, std::memory_order_relaxed);
        if (on_error) on_error(message);
    }

    void run() {
        int backoff_seconds = 1;
        bool first_attempt = true;
        beast::flat_buffer buffer;
        buffer.reserve(256U * 1024U);

        while (!stop_requested.load(std::memory_order_relaxed)) {
            if (!first_attempt) reconnects.fetch_add(1, std::memory_order_relaxed);
            first_attempt = false;
            bool marked_connected = false;
            try {
                asio::io_context io;
                ssl::context tls(ssl::context::tls_client);
                tls.set_default_verify_paths();
                tls.set_verify_mode(ssl::verify_peer);

                websocket::stream<beast::ssl_stream<beast::tcp_stream>> ws(io, tls);
                const std::string host(kUserHost);
                if (!::SSL_set_tlsext_host_name(ws.next_layer().native_handle(), host.c_str())) {
                    throw std::runtime_error("user WS SNI setup failed: " + openssl_error());
                }
#if PM_USER_WS_HOST_VERIFY
                ws.next_layer().set_verify_callback(ssl::host_name_verification(host));
#else
                ws.next_layer().set_verify_callback(ssl::rfc2818_verification(host));
#endif

                tcp::resolver resolver(io);
                const auto resolved = resolver.resolve(host, std::string(kUserPort));
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
                ws.handshake(host, std::string(kUserTarget));
                ws.text(true);
                ws.write(asio::buffer(subscription_json));

                connected.store(true, std::memory_order_release);
                marked_connected = true;
                backoff_seconds = 1;

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
                        const auto stamp = receive_stamp();
                        if (error) {
                            finish(error);
                            return;
                        }
                        const auto front = beast::buffers_front(buffer.data());
                        const std::string_view message{
                            static_cast<const char*>(front.data()), front.size()};
                        if (message != "PONG" && !message.empty()) {
                            messages.fetch_add(1, std::memory_order_relaxed);
                            on_message(message, stamp);
                        }
                        begin_read();
                    });
                };

                schedule_ping = [&] {
                    if (terminal || stop_requested.load(std::memory_order_relaxed)) return;
                    heartbeat.expires_after(std::chrono::seconds(10));
                    heartbeat.async_wait([&](beast::error_code error) {
                        if (error || terminal
                            || stop_requested.load(std::memory_order_relaxed)) return;
                        ws.text(true);
                        ws.async_write(asio::buffer(std::string_view{"PING"}),
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

UserWebSocketFeed::UserWebSocketFeed(UserWsCredentials credentials,
                                     std::vector<std::string> markets,
                                     MessageHandler on_message,
                                     ErrorHandler on_error)
    : impl_(std::make_unique<Impl>(std::move(credentials), std::move(markets),
                                   std::move(on_message), std::move(on_error))) {}

UserWebSocketFeed::~UserWebSocketFeed() { stop(); }
void UserWebSocketFeed::start() { impl_->start(); }
void UserWebSocketFeed::stop() { impl_->stop(); }

UserWsSnapshot UserWebSocketFeed::snapshot() const noexcept {
    return UserWsSnapshot{
        static_cast<std::uint8_t>(impl_->connected.load(std::memory_order_acquire)),
        impl_->messages.load(std::memory_order_relaxed),
        impl_->reconnects.load(std::memory_order_relaxed),
        impl_->errors.load(std::memory_order_relaxed),
    };
}

} // namespace pm::v7
