#include "pm/fast_ws.hpp"

#include <boost/asio/connect.hpp>
#include <boost/asio/ip/tcp.hpp>
#include <boost/asio/ssl/context.hpp>
#include <boost/asio/ssl/error.hpp>
#include <boost/asio/ssl/stream.hpp>
#include <boost/asio/ssl/rfc2818_verification.hpp>
#include <boost/beast/core.hpp>
#include <boost/beast/ssl.hpp>
#include <boost/beast/websocket.hpp>
#include <boost/json.hpp>
#include <openssl/err.h>
#include <openssl/ssl.h>

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <fstream>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <utility>

namespace pm::fast {
namespace {
namespace asio = boost::asio;
namespace beast = boost::beast;
namespace websocket = beast::websocket;
namespace ssl = asio::ssl;
namespace json = boost::json;
using tcp = asio::ip::tcp;

std::int64_t now_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

struct Endpoint {
    std::string host;
    std::string port = "443";
    std::string target = "/";
};

Endpoint parse_wss_url(std::string url) {
    constexpr std::string_view scheme = "wss://";
    if (!url.starts_with(scheme)) {
        throw std::runtime_error("fast market feed requires a wss:// URL");
    }
    url.erase(0, scheme.size());
    const auto slash = url.find('/');
    std::string authority = slash == std::string::npos ? url : url.substr(0, slash);
    Endpoint endpoint;
    endpoint.target = slash == std::string::npos ? "/" : url.substr(slash);
    if (authority.empty()) throw std::runtime_error("WebSocket URL has no host");

    if (authority.front() == '[') {
        const auto closing = authority.find(']');
        if (closing == std::string::npos) throw std::runtime_error("invalid IPv6 WebSocket URL");
        endpoint.host = authority.substr(1, closing - 1);
        if (closing + 1 < authority.size()) {
            if (authority[closing + 1] != ':') throw std::runtime_error("invalid WebSocket authority");
            endpoint.port = authority.substr(closing + 2);
        }
    } else {
        const auto colon = authority.rfind(':');
        if (colon != std::string::npos && authority.find(':') == colon) {
            endpoint.host = authority.substr(0, colon);
            endpoint.port = authority.substr(colon + 1);
        } else {
            endpoint.host = authority;
        }
    }
    if (endpoint.host.empty() || endpoint.port.empty()) {
        throw std::runtime_error("invalid WebSocket endpoint");
    }
    return endpoint;
}

std::string subscription(const std::vector<std::string>& ids) {
    json::array assets;
    assets.reserve(ids.size());
    for (const auto& id : ids) assets.emplace_back(id);
    return json::serialize(json::object{
        {"assets_ids", std::move(assets)},
        {"type", "market"},
        {"custom_feature_enabled", true},
    });
}

std::string openssl_error() {
    const auto code = ::ERR_get_error();
    if (code == 0) return "unknown TLS error";
    char buffer[256]{};
    ::ERR_error_string_n(code, buffer, sizeof(buffer));
    return buffer;
}

} // namespace

struct MarketWebSocketFeed::Impl {
    Endpoint endpoint;
    std::vector<std::vector<std::string>> shards;
    MessageHandler on_message;
    ErrorHandler on_error;
    std::vector<std::jthread> threads;
    std::atomic<bool> started{false};
    std::atomic<std::size_t> connected{0};
    std::atomic<std::uint64_t> messages{0};
    std::atomic<std::uint64_t> reconnects{0};
    std::atomic<std::uint64_t> errors{0};
    std::ofstream capture;
    std::mutex capture_mutex;

    Impl(std::string url, std::vector<std::string> ids, std::size_t shard_size,
         MessageHandler message_handler, ErrorHandler error_handler)
        : endpoint(parse_wss_url(std::move(url))), on_message(std::move(message_handler)),
          on_error(std::move(error_handler)) {
        ids.erase(std::remove_if(ids.begin(), ids.end(), [](const std::string& id) {
            return id.empty();
        }), ids.end());
        std::sort(ids.begin(), ids.end());
        ids.erase(std::unique(ids.begin(), ids.end()), ids.end());
        const std::size_t width = std::max<std::size_t>(1, shard_size);
        for (std::size_t pos = 0; pos < ids.size(); pos += width) {
            const auto end = std::min(ids.size(), pos + width);
            shards.emplace_back(ids.begin() + static_cast<std::ptrdiff_t>(pos),
                                ids.begin() + static_cast<std::ptrdiff_t>(end));
        }
        if (const char* path = std::getenv("POLYMARKET_FAST_WS_CAPTURE"); path && *path) {
            capture.open(path, std::ios::app);
            if (!capture) throw std::runtime_error("cannot open optional fast WebSocket capture");
        }
    }

    void report(std::size_t shard, std::string_view message) {
        errors.fetch_add(1, std::memory_order_relaxed);
        if (on_error) on_error(shard, message);
    }

    void capture_message(std::int64_t received_ms, std::size_t shard, const std::string& message) {
        if (!capture.is_open() || message.empty() || (message.front() != '{' && message.front() != '[')) {
            return;
        }
        std::scoped_lock lock(capture_mutex);
        capture << "{\"received_ts_ms\":" << received_ms << ",\"shard\":" << shard
                << ",\"payload\":" << message << "}\n";
    }

    void run_worker(std::stop_token stop, std::size_t shard_index,
                    const std::vector<std::string>& ids) {
        int backoff_seconds = 1;
        bool first_attempt = true;
        while (!stop.stop_requested()) {
            if (!first_attempt) reconnects.fetch_add(1, std::memory_order_relaxed);
            first_attempt = false;
            bool marked_connected = false;
            try {
                asio::io_context io;
                ssl::context tls(ssl::context::tls_client);
                tls.set_default_verify_paths();
                tls.set_verify_mode(ssl::verify_peer);

                tcp::resolver resolver(io);
                websocket::stream<beast::ssl_stream<beast::tcp_stream>> ws(io, tls);
                if (!::SSL_set_tlsext_host_name(ws.next_layer().native_handle(),
                                                endpoint.host.c_str())) {
                    throw std::runtime_error("SNI setup failed: " + openssl_error());
                }
                ws.next_layer().set_verify_callback(ssl::rfc2818_verification(endpoint.host));

                const auto resolved = resolver.resolve(endpoint.host, endpoint.port);
                beast::get_lowest_layer(ws).expires_after(std::chrono::seconds(10));
                beast::get_lowest_layer(ws).connect(resolved);
                ws.next_layer().handshake(ssl::stream_base::client);

                beast::get_lowest_layer(ws).expires_never();
                websocket::stream_base::timeout timeouts{
                    std::chrono::seconds(10),
                    std::chrono::seconds(20),
                    true,
                };
                ws.set_option(timeouts);
                ws.set_option(websocket::stream_base::decorator([](websocket::request_type& request) {
                    request.set(beast::http::field::user_agent,
                                "polymarket-fast-arb-shadow/0.9");
                }));
                ws.handshake(endpoint.host, endpoint.target);
                ws.text(true);
                ws.write(asio::buffer(subscription(ids)));

                connected.fetch_add(1, std::memory_order_relaxed);
                marked_connected = true;
                backoff_seconds = 1;
                std::int64_t last_text_ping = now_ms();

                std::stop_callback cancel_on_stop(stop, [&ws] {
                    beast::error_code ignored;
                    beast::get_lowest_layer(ws).socket().cancel(ignored);
                    beast::get_lowest_layer(ws).socket().shutdown(tcp::socket::shutdown_both, ignored);
                    beast::get_lowest_layer(ws).socket().close(ignored);
                });

                while (!stop.stop_requested()) {
                    beast::flat_buffer buffer;
                    beast::error_code error;
                    ws.read(buffer, error);
                    if (error) {
                        if (!stop.stop_requested() && error != websocket::error::closed) {
                            throw beast::system_error(error);
                        }
                        break;
                    }
                    const auto received_ms = now_ms();
                    std::string message = beast::buffers_to_string(buffer.data());
                    if (message != "PONG" && !message.empty()) {
                        messages.fetch_add(1, std::memory_order_relaxed);
                        capture_message(received_ms, shard_index, message);
                        on_message(message, received_ms, shard_index);
                    }
                    if (received_ms - last_text_ping >= 10000) {
                        ws.text(true);
                        ws.write(asio::buffer(std::string_view{"PING"}), error);
                        if (error) throw beast::system_error(error);
                        last_text_ping = received_ms;
                    }
                }
            } catch (const std::exception& error) {
                if (!stop.stop_requested()) report(shard_index, error.what());
            }

            if (marked_connected) connected.fetch_sub(1, std::memory_order_relaxed);
            if (!stop.stop_requested()) {
                std::this_thread::sleep_for(std::chrono::seconds(backoff_seconds));
                backoff_seconds = std::min(30, backoff_seconds * 2);
            }
        }
    }

    void start() {
        bool expected = false;
        if (!started.compare_exchange_strong(expected, true)) return;
        threads.reserve(shards.size());
        for (std::size_t i = 0; i < shards.size(); ++i) {
            threads.emplace_back([this, i, ids = shards[i]](std::stop_token stop) {
                run_worker(stop, i, ids);
            });
        }
    }

    void stop() {
        if (!started.exchange(false)) return;
        for (auto& thread : threads) thread.request_stop();
        threads.clear();
        if (capture.is_open()) capture.flush();
    }
};

MarketWebSocketFeed::MarketWebSocketFeed(std::string url,
                                         std::vector<std::string> asset_ids,
                                         std::size_t shard_size,
                                         MessageHandler on_message,
                                         ErrorHandler on_error)
    : impl_(std::make_unique<Impl>(std::move(url), std::move(asset_ids), shard_size,
                                   std::move(on_message), std::move(on_error))) {}

MarketWebSocketFeed::~MarketWebSocketFeed() { stop(); }

void MarketWebSocketFeed::start() { impl_->start(); }
void MarketWebSocketFeed::stop() { impl_->stop(); }

FeedSnapshot MarketWebSocketFeed::snapshot() const {
    return FeedSnapshot{
        impl_->shards.size(),
        impl_->connected.load(std::memory_order_relaxed),
        impl_->messages.load(std::memory_order_relaxed),
        impl_->reconnects.load(std::memory_order_relaxed),
        impl_->errors.load(std::memory_order_relaxed),
    };
}

} // namespace pm::fast
