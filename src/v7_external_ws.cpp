#include "pm/v7_external_ws.hpp"

#include <boost/asio/connect.hpp>
#include <boost/asio/ip/tcp.hpp>
#include <boost/asio/ssl/context.hpp>
#include <boost/asio/ssl/stream.hpp>
#include <boost/beast/core.hpp>
#include <boost/beast/core/flat_static_buffer.hpp>
#include <boost/beast/ssl.hpp>
#include <boost/beast/websocket.hpp>
#include <openssl/ssl.h>

#include <algorithm>
#include <chrono>
#include <stdexcept>
#include <thread>

namespace pm::v7::external_fair {
namespace {
namespace net = boost::asio;
namespace ssl = net::ssl;
namespace beast = boost::beast;
namespace websocket = beast::websocket;
using tcp = net::ip::tcp;

constexpr std::size_t kMaxWsMessageBytes = 1U << 20;

[[nodiscard]] std::int64_t monotonic_now_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

[[nodiscard]] std::int64_t wall_now_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}

[[nodiscard]] std::string normalize_host_for_handshake(const std::string& host) {
    return host;
}

void bounded_backoff(ExternalStopToken stop, std::uint64_t failures) noexcept {
    const auto capped = std::min<std::uint64_t>(failures, 5);
    const auto delay = std::chrono::milliseconds(100ULL << capped);
    const auto deadline = std::chrono::steady_clock::now() + delay;
    while (!stop.stop_requested() && std::chrono::steady_clock::now() < deadline) {
        std::this_thread::sleep_for(std::chrono::milliseconds(25));
    }
}

} // namespace

ExternalVenueConnectionSpec btc_spot_connection_spec(
    VenueId venue,
    std::uint64_t asset_handle) {
    if (asset_handle == 0) throw std::invalid_argument("asset_handle must be non-zero");

    ExternalVenueConnectionSpec spec;
    spec.venue = venue;
    spec.asset_handle = asset_handle;
    spec.max_message_bytes = kMaxWsMessageBytes;
    switch (venue) {
        case VenueId::BinanceSpot:
            spec.host = "stream.binance.com";
            spec.port = "9443";
            spec.target = "/ws";
            spec.symbol = "BTCUSDT";
            spec.subscription_json =
                R"({"method":"SUBSCRIBE","params":["btcusdt@bookTicker","btcusdt@aggTrade"],"id":1})";
            break;
        case VenueId::CoinbaseSpot:
            spec.host = "advanced-trade-ws.coinbase.com";
            spec.port = "443";
            spec.target = "/";
            spec.symbol = "BTC-USD";
            // Coinbase requires one subscribe message per channel. A JSON array
            // here would not be a valid protocol message, so the IO loop emits
            // the two newline-delimited objects separately before reading.
            spec.subscription_json =
                R"({"type":"subscribe","product_ids":["BTC-USD"],"channel":"ticker"})"
                "\n"
                R"({"type":"subscribe","product_ids":["BTC-USD"],"channel":"market_trades"})";
            break;
        case VenueId::BybitSpot:
            spec.host = "stream.bybit.com";
            spec.port = "443";
            spec.target = "/v5/public/spot";
            spec.symbol = "BTCUSDT";
            spec.subscription_json =
                R"({"op":"subscribe","args":["orderbook.1.BTCUSDT","publicTrade.BTCUSDT"]})";
            break;
        case VenueId::Unknown:
        default:
            throw std::invalid_argument("unsupported external venue");
    }
    return spec;
}

ExternalVenueWsClient::ExternalVenueWsClient(
    ExternalVenueConnectionSpec spec,
    ExternalVenueIngress& ingress)
    : spec_(std::move(spec)), ingress_(ingress) {
    if (spec_.venue == VenueId::Unknown || spec_.asset_handle == 0
        || spec_.host.empty() || spec_.port.empty() || spec_.target.empty()
        || spec_.subscription_json.empty() || spec_.max_message_bytes == 0
        || spec_.max_message_bytes > kMaxWsMessageBytes) {
        throw std::invalid_argument("invalid external venue connection spec");
    }
}

void ExternalVenueWsClient::run(ExternalStopToken stop) noexcept {
    std::uint64_t consecutive_failures = 0;
    while (!stop.stop_requested()) {
        connection_attempts_.fetch_add(1, std::memory_order_relaxed);
        const auto epoch = connection_epoch_.fetch_add(1, std::memory_order_acq_rel) + 1;
        try {
            net::io_context io;
            ssl::context context(ssl::context::tls_client);
            context.set_default_verify_paths();
            context.set_verify_mode(ssl::verify_peer);

            tcp::resolver resolver(io);
            websocket::stream<beast::ssl_stream<beast::tcp_stream>> ws(io, context);
            const auto endpoints = resolver.resolve(spec_.host, spec_.port);
            beast::get_lowest_layer(ws).connect(endpoints);

            if (!SSL_set_tlsext_host_name(ws.next_layer().native_handle(), spec_.host.c_str())) {
                throw beast::system_error(
                    static_cast<int>(::ERR_get_error()), net::error::get_ssl_category());
            }
            ws.next_layer().handshake(ssl::stream_base::client);
            ws.set_option(websocket::stream_base::timeout::suggested(beast::role_type::client));
            ws.read_message_max(spec_.max_message_bytes);
            ws.handshake(normalize_host_for_handshake(spec_.host), spec_.target);

            // Protocol messages remain on the IO thread and are never used as a
            // trading trigger. Coinbase needs two channel subscriptions.
            std::size_t start = 0;
            while (start < spec_.subscription_json.size()) {
                const auto end = spec_.subscription_json.find('\n', start);
                const auto length = end == std::string::npos
                    ? spec_.subscription_json.size() - start : end - start;
                if (length > 0) {
                    ws.write(net::buffer(spec_.subscription_json.data() + start, length));
                }
                if (end == std::string::npos) break;
                start = end + 1;
            }

            connected_.store(true, std::memory_order_release);
            healthy_.store(true, std::memory_order_release);
            successful_connections_.fetch_add(1, std::memory_order_relaxed);
            consecutive_failures = 0;

            while (!stop.stop_requested()) {
                beast::flat_static_buffer<kMaxWsMessageBytes> buffer;
                ws.read(buffer);
                const auto receive_ns = monotonic_now_ns();
                const auto wall_ns = wall_now_ns();
                frames_received_.fetch_add(1, std::memory_order_relaxed);
                last_receive_monotonic_ns_.store(receive_ns, std::memory_order_release);
                last_receive_wall_ns_.store(wall_ns, std::memory_order_release);

                const auto sequence = buffer.data();
                const auto front = beast::buffers_front(sequence);
                const auto total = net::buffer_size(sequence);
                if (front.size() != total) {
                    decode_failures_.fetch_add(1, std::memory_order_relaxed);
                    ingress_.mark_disconnected(epoch + 1);
                    healthy_.store(false, std::memory_order_release);
                    throw std::runtime_error("non-contiguous bounded websocket frame");
                }
                const auto* bytes = static_cast<const char*>(front.data());
                const std::string_view payload(bytes, front.size());
                const auto decoded = ingress_.on_frame(
                    epoch, receive_ns, wall_ns, payload);
                if (decoded.invalid_frame != 0 || decoded.output_overflow != 0
                    || decoded.arena_exhausted != 0) {
                    decode_failures_.fetch_add(1, std::memory_order_relaxed);
                }
            }

            boost::system::error_code close_error;
            ws.close(websocket::close_code::normal, close_error);
            connected_.store(false, std::memory_order_release);
            healthy_.store(false, std::memory_order_release);
            if (stop.stop_requested()) return;
        } catch (...) {
            connected_.store(false, std::memory_order_release);
            healthy_.store(false, std::memory_order_release);
            transport_failures_.fetch_add(1, std::memory_order_relaxed);
            ingress_.mark_disconnected(epoch);
            ++consecutive_failures;
            bounded_backoff(stop, consecutive_failures);
        }
    }
}

ExternalWsSnapshot ExternalVenueWsClient::snapshot() const noexcept {
    ExternalWsSnapshot out;
    out.connection_epoch = connection_epoch_.load(std::memory_order_acquire);
    out.connection_attempts = connection_attempts_.load(std::memory_order_acquire);
    out.successful_connections = successful_connections_.load(std::memory_order_acquire);
    out.frames_received = frames_received_.load(std::memory_order_acquire);
    out.transport_failures = transport_failures_.load(std::memory_order_acquire);
    out.decode_failures = decode_failures_.load(std::memory_order_acquire);
    out.last_receive_monotonic_ns = last_receive_monotonic_ns_.load(std::memory_order_acquire);
    out.last_receive_wall_ns = last_receive_wall_ns_.load(std::memory_order_acquire);
    out.connected = connected_.load(std::memory_order_acquire) ? 1 : 0;
    out.healthy = healthy_.load(std::memory_order_acquire) ? 1 : 0;
    return out;
}

} // namespace pm::v7::external_fair
