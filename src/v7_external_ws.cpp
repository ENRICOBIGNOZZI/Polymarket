#include "pm/v7_external_ws.hpp"
#include "pm/v7_external_tape.hpp"

#include <boost/asio/connect.hpp>
#include <boost/asio/ip/tcp.hpp>
#include <boost/asio/ssl/context.hpp>
#include <boost/asio/ssl/stream.hpp>
#include <boost/beast/core.hpp>
#include <boost/beast/core/flat_static_buffer.hpp>
#include <boost/beast/ssl.hpp>
#include <boost/beast/websocket.hpp>
#include <boost/json.hpp>
#include <openssl/ssl.h>
#include <openssl/evp.h>
#include <openssl/hmac.h>

#include <algorithm>
#include <chrono>
#include <iostream>
#include <iomanip>
#include <memory>
#include <stdexcept>
#include <sstream>
#include <thread>
#include <vector>

namespace pm::v7::external_fair {
namespace {
namespace net = boost::asio;
namespace ssl = net::ssl;
namespace beast = boost::beast;
namespace websocket = beast::websocket;
namespace json = boost::json;
using tcp = net::ip::tcp;

// Coinbase's documented public L2 feed begins with a complete BTC-USD book
// snapshot. A live snapshot can exceed 1 MiB, so retain a bounded cap while
// admitting the full recovery frame instead of reconnecting indefinitely.
constexpr std::size_t kMaxWsMessageBytes = 2U << 20;

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

std::vector<unsigned char> base64_decode(std::string_view encoded) {
    if (encoded.empty() || encoded.size() % 4 != 0) throw std::invalid_argument("invalid base64 secret");
    std::vector<unsigned char> out((encoded.size() / 4) * 3 + 3);
    const int decoded = EVP_DecodeBlock(out.data(),
        reinterpret_cast<const unsigned char*>(encoded.data()), static_cast<int>(encoded.size()));
    if (decoded < 0) throw std::invalid_argument("invalid base64 secret");
    std::size_t size = static_cast<std::size_t>(decoded);
    if (!encoded.empty() && encoded.back() == '=') --size;
    if (encoded.size() > 1 && encoded[encoded.size() - 2] == '=') --size;
    out.resize(size);
    return out;
}

std::string base64_encode(const unsigned char* data, std::size_t size) {
    std::string out(4 * ((size + 2) / 3), '\0');
    const int encoded = EVP_EncodeBlock(reinterpret_cast<unsigned char*>(out.data()),
                                        data, static_cast<int>(size));
    if (encoded < 0) throw std::runtime_error("base64 encode failed");
    out.resize(static_cast<std::size_t>(encoded));
    return out;
}

std::string coinbase_timestamp() {
    const auto microseconds = std::chrono::duration_cast<std::chrono::microseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
    std::ostringstream out;
    out << (microseconds / 1'000'000LL) << '.' << std::setw(6) << std::setfill('0')
        << (microseconds % 1'000'000LL);
    return out.str();
}

std::string coinbase_level2_subscription(std::string_view product, std::string_view api_key,
                                         std::string_view secret_b64, std::string_view passphrase,
                                         std::string_view timestamp) {
    if (product.empty() || api_key.empty() || secret_b64.empty() || passphrase.empty() || timestamp.empty())
        throw std::invalid_argument("Coinbase Exchange Level2 credentials/identity required");
    const auto key = base64_decode(secret_b64);
    const std::string message = std::string(timestamp) + "GET/users/self/verify";
    unsigned char digest[EVP_MAX_MD_SIZE]{}; unsigned int digest_size = 0;
    if (HMAC(EVP_sha256(), key.data(), static_cast<int>(key.size()),
             reinterpret_cast<const unsigned char*>(message.data()), message.size(),
             digest, &digest_size) == nullptr) {
        throw std::runtime_error("Coinbase Exchange signature failed");
    }
    json::object channel{{"name", "level2"}, {"product_ids", json::array{std::string(product)}}};
    json::object request{
        {"type", "subscribe"}, {"channels", json::array{std::move(channel)}},
        {"signature", base64_encode(digest, digest_size)}, {"key", api_key},
        {"passphrase", passphrase}, {"timestamp", timestamp},
    };
    return json::serialize(request);
}

std::string connection_subscription(const ExternalVenueConnectionSpec& spec) {
    if (spec.subscription_auth == ExternalWsSubscriptionAuth::None) return spec.subscription_json;
    if (spec.subscription_auth != ExternalWsSubscriptionAuth::CoinbaseExchangeLevel2)
        throw std::invalid_argument("unsupported external subscription auth");
    const char* api_key = std::getenv("COINBASE_EXCHANGE_API_KEY");
    const char* secret = std::getenv("COINBASE_EXCHANGE_API_SECRET");
    const char* passphrase = std::getenv("COINBASE_EXCHANGE_PASSPHRASE");
    if (!api_key || !*api_key || !secret || !*secret || !passphrase || !*passphrase)
        throw std::runtime_error("Coinbase Exchange Level2 credential environment incomplete");
    return coinbase_level2_subscription(spec.symbol, api_key, secret, passphrase, coinbase_timestamp());
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
                R"({"method":"SUBSCRIBE","params":["btcusdt@depth@100ms","btcusdt@bookTicker","btcusdt@aggTrade"],"id":1})";
            break;
        case VenueId::CoinbaseSpot:
            spec.host = "ws-feed.exchange.coinbase.com";
            spec.port = "443";
            spec.target = "/";
            spec.symbol = "BTC-USD";
            spec.subscription_json =
                // Coinbase Exchange documents level2_batch as the public,
                // unauthenticated L2 channel; it has the same snapshot and
                // absolute-size update semantics as level2.
                R"({"type":"subscribe","product_ids":["BTC-USD"],"channels":["level2_batch"]})";
            break;
        case VenueId::BybitSpot:
            spec.host = "stream.bybit.com";
            spec.port = "443";
            spec.target = "/v5/public/spot";
            spec.symbol = "BTCUSDT";
            spec.subscription_json =
                R"({"op":"subscribe","args":["orderbook.50.BTCUSDT","publicTrade.BTCUSDT"]})";
            break;
        case VenueId::BinanceUsdM:
            spec.host = "fstream.binance.com";
            spec.port = "443";
            // Binance Futures separates high-frequency books (/public) from
            // aggregate trades, marks, and liquidations (/market). The
            // runtime splits those stream classes onto separate clients.
            spec.target = "/public/ws";
            spec.symbol = "BTCUSDT";
            spec.subscription_json =
                R"({"method":"SUBSCRIBE","params":["btcusdt@depth20@100ms","btcusdt@aggTrade","btcusdt@markPrice@1s","btcusdt@forceOrder"],"id":1})";
            break;
        case VenueId::Deribit:
            spec.host = "www.deribit.com";
            spec.port = "443";
            spec.target = "/ws/api/v2";
            spec.symbol = "BTC-PERPETUAL";
            spec.subscription_json =
                R"({"jsonrpc":"2.0","method":"public/subscribe","id":1,"params":{"channels":["ticker.BTC-PERPETUAL.100ms","trades.BTC-PERPETUAL.100ms"]}})";
            break;
        case VenueId::BybitLinear:
            spec.host = "stream.bybit.com";
            spec.port = "443";
            spec.target = "/v5/public/linear";
            spec.symbol = "BTCUSDT";
            spec.subscription_json =
                R"({"op":"subscribe","args":["orderbook.50.BTCUSDT","publicTrade.BTCUSDT","tickers.BTCUSDT","allLiquidation.BTCUSDT"]})";
            break;
        case VenueId::Unknown:
        default:
            throw std::invalid_argument("unsupported external venue");
    }
    return spec;
}

ExternalVenueConnectionSpec coinbase_level2_connection_spec(std::uint64_t asset_handle) {
    auto spec = btc_spot_connection_spec(VenueId::CoinbaseSpot, asset_handle);
    spec.subscription_json.clear();
    spec.subscription_auth = ExternalWsSubscriptionAuth::CoinbaseExchangeLevel2;
    return spec;
}

std::string coinbase_level2_subscription_for_test(
    std::string_view product, std::string_view api_key, std::string_view secret_b64,
    std::string_view passphrase, std::string_view timestamp) {
    return coinbase_level2_subscription(product, api_key, secret_b64, passphrase, timestamp);
}

ExternalVenueWsClient::ExternalVenueWsClient(
    ExternalVenueConnectionSpec spec,
    ExternalVenueIngress* ingress, ExternalFrameObserver* observer,
    ExternalRawFrameSink* raw_sink)
    : spec_(std::move(spec)), ingress_(ingress), observer_(observer), raw_sink_(raw_sink) {
    if (spec_.venue == VenueId::Unknown || spec_.asset_handle == 0
        || spec_.host.empty() || spec_.port.empty() || spec_.target.empty()
        || (spec_.subscription_json.empty() && spec_.subscription_auth == ExternalWsSubscriptionAuth::None
            && !spec_.start_without_subscription)
        || spec_.max_message_bytes == 0
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
            // Do not hold small subscription/control frames for Nagle batching.
            beast::get_lowest_layer(ws).socket().set_option(tcp::no_delay(true));

            if (!SSL_set_tlsext_host_name(ws.next_layer().native_handle(), spec_.host.c_str())) {
                throw beast::system_error(
                    static_cast<int>(::ERR_get_error()), net::error::get_ssl_category());
            }
            ws.next_layer().handshake(ssl::stream_base::client);
            // Bound an otherwise blocking read so process shutdown can drain
            // writer queues and leave complete immutable tape records.
            websocket::stream_base::timeout timeout;
            timeout.handshake_timeout = std::chrono::seconds(30);
            timeout.idle_timeout = std::chrono::seconds(1);
            timeout.keep_alive_pings = true;
            ws.set_option(timeout);
            if (!spec_.handshake_headers.empty()) {
                const auto headers = spec_.handshake_headers;
                ws.set_option(websocket::stream_base::decorator(
                    [headers](websocket::request_type& request) {
                        for (const auto& [name, value] : headers) request.set(name, value);
                    }));
            }
            ws.read_message_max(spec_.max_message_bytes);
            ws.handshake(normalize_host_for_handshake(spec_.host), spec_.target);

            std::atomic<bool> session_done{false};
            std::thread stop_watcher([&] {
                while (!session_done.load(std::memory_order_acquire)
                       && !stop.stop_requested()) {
                    std::this_thread::sleep_for(std::chrono::milliseconds(20));
                }
                if (!session_done.load(std::memory_order_acquire) && stop.stop_requested()) {
                    boost::system::error_code ignored;
                    auto& socket = beast::get_lowest_layer(ws).socket();
                    socket.shutdown(tcp::socket::shutdown_both, ignored);
                    socket.close(ignored);
                }
            });
            const auto finish_session = [&] {
                session_done.store(true, std::memory_order_release);
                if (stop_watcher.joinable()) stop_watcher.join();
            };

            try {

            // Protocol messages remain on the IO thread and are never used as a
            // trading trigger. Authenticated subscriptions are generated fresh
            // per connection so Coinbase timestamps/signatures are never reused.
            const auto subscription = connection_subscription(spec_);
            std::size_t start = 0;
            while (start < subscription.size()) {
                const auto end = subscription.find('\n', start);
                const auto length = end == std::string::npos
                    ? subscription.size() - start : end - start;
                if (length > 0) {
                    ws.write(net::buffer(subscription.data() + start, length));
                }
                if (end == std::string::npos) break;
                start = end + 1;
            }

            connected_.store(true, std::memory_order_release);
            healthy_.store(true, std::memory_order_release);
            successful_connections_.fetch_add(1, std::memory_order_relaxed);
            if (observer_ != nullptr) observer_->on_connection_epoch(epoch);
            consecutive_failures = 0;
            // macOS worker threads default to a 512 KiB stack. The bounded
            // 1 MiB Beast buffer must therefore live on the heap once per
            // connection, not on every read-loop stack frame.
            auto buffer = std::make_unique<beast::flat_static_buffer<kMaxWsMessageBytes>>();

            while (!stop.stop_requested()) {
                buffer->consume(buffer->size());
                // tcp_stream deadlines apply below Beast's WebSocket layer.
                // They bound a silent synchronous read so a SIGTERM-driven
                // stop request cannot leave the runtime waiting forever.
                beast::get_lowest_layer(ws).expires_after(std::chrono::seconds(1));
                ws.read(*buffer);
                beast::get_lowest_layer(ws).expires_never();
                const auto receive_ns = monotonic_now_ns();
                const auto wall_ns = wall_now_ns();
                frames_received_.fetch_add(1, std::memory_order_relaxed);
                last_receive_monotonic_ns_.store(receive_ns, std::memory_order_release);
                last_receive_wall_ns_.store(wall_ns, std::memory_order_release);

                const auto sequence = buffer->data();
                const auto front = beast::buffers_front(sequence);
                const auto total = net::buffer_size(sequence);
                if (front.size() != total) {
                    decode_failures_.fetch_add(1, std::memory_order_relaxed);
                    if (ingress_ != nullptr) ingress_->mark_disconnected(epoch + 1);
                    healthy_.store(false, std::memory_order_release);
                    throw std::runtime_error("non-contiguous bounded websocket frame");
                }
                const auto* bytes = static_cast<const char*>(front.data());
                const std::string_view payload(bytes, front.size());
                if (raw_sink_ != nullptr) (void)raw_sink_->try_record_raw(
                    spec_.venue, epoch, receive_ns, wall_ns, payload);
                const bool binary_frame = ws.got_binary();
                if (observer_ != nullptr) {
                    if (binary_frame) observer_->on_binary_frame(epoch, receive_ns, wall_ns, payload);
                    else observer_->on_frame(epoch, receive_ns, wall_ns, payload);
                }
                if (ingress_ != nullptr) {
                    if (binary_frame) {
                        decode_failures_.fetch_add(1, std::memory_order_relaxed);
                        ingress_->mark_disconnected(epoch + 1);
                        throw std::runtime_error("unexpected binary frame on JSON ingress");
                    }
                    const auto decoded = ingress_->on_frame(epoch, receive_ns, wall_ns, payload);
                    if (decoded.invalid_frame != 0 || decoded.output_overflow != 0
                        || decoded.arena_exhausted != 0) {
                        decode_failures_.fetch_add(1, std::memory_order_relaxed);
                    }
                }
            }

            } catch (...) {
                finish_session();
                throw;
            }
            finish_session();

            connected_.store(false, std::memory_order_release);
            healthy_.store(false, std::memory_order_release);
            // The process is already stopping. Do not wait for a peer close
            // acknowledgement here: an unresponsive server must not prevent
            // tape workers from draining and flushing their accepted frames.
            if (stop.stop_requested()) return;
            boost::system::error_code close_error;
            ws.close(websocket::close_code::normal, close_error);
        } catch (const std::exception& error) {
            connected_.store(false, std::memory_order_release);
            healthy_.store(false, std::memory_order_release);
            if (stop.stop_requested()) return;
            transport_failures_.fetch_add(1, std::memory_order_relaxed);
            if (ingress_ != nullptr) ingress_->mark_disconnected(epoch);
            std::cerr << "v7 external websocket transport failure venue="
                      << static_cast<unsigned>(spec_.venue) << " reason=" << error.what() << '\n';
            ++consecutive_failures;
            bounded_backoff(stop, consecutive_failures);
        } catch (...) {
            connected_.store(false, std::memory_order_release);
            healthy_.store(false, std::memory_order_release);
            if (stop.stop_requested()) return;
            transport_failures_.fetch_add(1, std::memory_order_relaxed);
            if (ingress_ != nullptr) ingress_->mark_disconnected(epoch);
            std::cerr << "v7 external websocket transport failure venue="
                      << static_cast<unsigned>(spec_.venue) << " reason=non_standard_exception\n";
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
