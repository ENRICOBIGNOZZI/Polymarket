#include "pm/v7_binance_l2.hpp"
#include "pm/v7_binance_l2_protocol.hpp"
#include "pm/v7_coinbase_l2.hpp"
#include "pm/v7_external_state.hpp"
#include "pm/v7_external_tape.hpp"
#include "pm/v7_external_ws.hpp"

#include <boost/asio/connect.hpp>
#include <boost/asio/ip/tcp.hpp>
#include <boost/asio/ssl/context.hpp>
#include <boost/asio/ssl/stream.hpp>
#include <boost/beast/core.hpp>
#include <boost/beast/http.hpp>
#include <boost/beast/ssl.hpp>
#include <boost/json.hpp>

#include <openssl/ssl.h>

#include <atomic>
#include <charconv>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <future>
#include <iostream>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <unistd.h>

namespace fs = std::filesystem;
namespace json = boost::json;
using namespace pm::v7::external_fair;

namespace {
namespace net = boost::asio;
namespace ssl = net::ssl;
namespace beast = boost::beast;
namespace http = beast::http;
using tcp = net::ip::tcp;
std::atomic<bool> stopping{false};

void signal_handler(int) noexcept { stopping.store(true, std::memory_order_relaxed); }

std::int64_t monotonic_now_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

std::int64_t wall_now_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}

void atomic_write(const fs::path& path, const json::object& value) {
    fs::create_directories(path.parent_path());
    const auto temporary = path.string() + ".tmp." + std::to_string(::getpid());
    {
        std::ofstream output(temporary, std::ios::binary | std::ios::trunc);
        if (!output) throw std::runtime_error("cannot open external venue status temporary");
        output << json::serialize(value) << '\n';
        output.flush();
        if (!output) throw std::runtime_error("cannot write external venue status");
    }
    fs::rename(temporary, path);
}

json::object transport_json(const ExternalWsSnapshot& value, const char* venue) {
    return {
        {"venue", venue},
        {"connection_epoch", value.connection_epoch},
        {"connection_attempts", value.connection_attempts},
        {"successful_connections", value.successful_connections},
        {"frames_received", value.frames_received},
        {"transport_failures", value.transport_failures},
        {"decode_failures", value.decode_failures},
        {"last_receive_monotonic_ns", value.last_receive_monotonic_ns},
        {"last_receive_wall_ns", value.last_receive_wall_ns},
        {"connected", value.connected != 0},
        {"healthy", value.healthy != 0},
    };
}

json::object tape_json(const TapeRecorderSnapshot& value, bool enabled) {
    return {
        {"enabled", enabled}, {"accepted", value.accepted}, {"written", value.written},
        {"dropped", value.dropped}, {"queued", value.queued},
        {"evidence_valid", value.evidence_valid != 0}, {"writer_healthy", value.writer_healthy != 0},
    };
}

const char* l2_state_name(BinanceL2State state) noexcept {
    switch (state) {
        case BinanceL2State::Buffering: return "BUFFERING";
        case BinanceL2State::Live: return "LIVE";
        case BinanceL2State::Gapped: return "GAPPED";
    }
    return "UNKNOWN";
}

json::object l2_json(const BinanceL2Metrics& value) {
    return {
        {"state", l2_state_name(value.state)}, {"valid", value.valid != 0},
        {"last_update_id", value.last_update_id},
        {"latest_receive_monotonic_ns", value.latest_receive_monotonic_ns},
        {"best_bid", value.best_bid}, {"best_ask", value.best_ask},
        {"mid", value.mid}, {"microprice", value.microprice},
        {"spread_bps", value.spread_bps},
        {"bid_depth_l1", value.bid_depth_l1}, {"ask_depth_l1", value.ask_depth_l1},
        {"bid_depth_l5", value.bid_depth_l5}, {"ask_depth_l5", value.ask_depth_l5},
        {"bid_depth_l10", value.bid_depth_l10}, {"ask_depth_l10", value.ask_depth_l10},
        {"bid_depth_l20", value.bid_depth_l20}, {"ask_depth_l20", value.ask_depth_l20},
        {"imbalance_l1", value.imbalance_l1}, {"imbalance_l5", value.imbalance_l5},
        {"imbalance_l10", value.imbalance_l10},
        {"bid_levels", value.bid_levels}, {"ask_levels", value.ask_levels},
    };
}

const char* l2_state_name(CoinbaseL2State state) noexcept {
    switch (state) {
        case CoinbaseL2State::AwaitingSnapshot: return "AWAITING_SNAPSHOT";
        case CoinbaseL2State::Live: return "LIVE";
        case CoinbaseL2State::Gapped: return "GAPPED";
    }
    return "UNKNOWN";
}

json::object l2_json(const CoinbaseL2Metrics& value) {
    return {
        {"state", l2_state_name(value.state)}, {"valid", value.valid != 0},
        {"update_count", value.update_count}, {"parse_failures", value.parse_failures},
        {"latest_receive_monotonic_ns", value.latest_receive_monotonic_ns},
        {"best_bid", value.best_bid}, {"best_ask", value.best_ask},
        {"mid", value.mid}, {"microprice", value.microprice},
        {"spread_bps", value.spread_bps},
        {"bid_depth_l1", value.bid_depth_l1}, {"ask_depth_l1", value.ask_depth_l1},
        {"bid_depth_l5", value.bid_depth_l5}, {"ask_depth_l5", value.ask_depth_l5},
        {"bid_depth_l10", value.bid_depth_l10}, {"ask_depth_l10", value.ask_depth_l10},
        {"bid_depth_l20", value.bid_depth_l20}, {"ask_depth_l20", value.ask_depth_l20},
        {"imbalance_l1", value.imbalance_l1}, {"imbalance_l5", value.imbalance_l5},
        {"imbalance_l10", value.imbalance_l10},
        {"bid_levels", value.bid_levels}, {"ask_levels", value.ask_levels},
    };
}

[[nodiscard]] const json::value* json_field(const json::object& object, std::string_view key) noexcept {
    const auto iter = object.find(key);
    return iter == object.end() ? nullptr : &iter->value();
}

[[nodiscard]] bool json_number(const json::value* value, double& output) noexcept {
    if (value == nullptr) return false;
    if (value->is_double()) output = value->as_double();
    else if (value->is_int64()) output = static_cast<double>(value->as_int64());
    else if (value->is_uint64()) output = static_cast<double>(value->as_uint64());
    else if (value->is_string()) {
        const auto& text = value->as_string();
        std::string copy(text.data(), text.size());
        char* end = nullptr;
        output = std::strtod(copy.c_str(), &end);
        if (end != copy.c_str() + copy.size()) return false;
    } else return false;
    return std::isfinite(output);
}

[[nodiscard]] bool json_text_equals(const json::value* value, std::string_view expected) noexcept {
    if (value == nullptr || !value->is_string()) return false;
    const auto& text = value->as_string();
    return std::string_view(text.data(), text.size()) == expected;
}

[[nodiscard]] bool json_bool(const json::value* value, bool& output) noexcept {
    if (value == nullptr || !value->is_bool()) return false;
    output = value->as_bool();
    return true;
}

[[nodiscard]] bool json_u64(const json::value* value, std::uint64_t& output) noexcept {
    if (value == nullptr) return false;
    if (value->is_uint64()) { output = value->as_uint64(); return true; }
    if (value->is_int64()) {
        if (value->as_int64() < 0) return false;
        output = static_cast<std::uint64_t>(value->as_int64());
        return true;
    }
    if (!value->is_string()) return false;
    const auto& text = value->as_string();
    const auto parsed = std::from_chars(text.data(), text.data() + text.size(), output);
    return parsed.ec == std::errc{} && parsed.ptr == text.data() + text.size();
}

struct BinanceUsdMMetrics {
    std::int64_t book_receive_ns = 0;
    std::int64_t mark_receive_ns = 0;
    double bid = 0.0;
    double ask = 0.0;
    double bid_depth_l20 = 0.0;
    double ask_depth_l20 = 0.0;
    double microprice = 0.0;
    double signed_trade_flow = 0.0;
    double mark_price = 0.0;
    double index_price = 0.0;
    double funding_rate = 0.0;
    double liquidation_buy_notional = 0.0;
    double liquidation_sell_notional = 0.0;
    std::uint64_t frames = 0;
    std::uint64_t parse_failures = 0;
    std::uint64_t depth_frames = 0;
    std::uint64_t trade_frames = 0;
    std::uint64_t mark_frames = 0;
    std::uint64_t liquidation_frames = 0;
    std::uint64_t ignored_frames = 0;
    std::uint8_t valid = 0;
};

json::object usdm_json(const BinanceUsdMMetrics& value) {
    return {
        {"valid", value.valid != 0}, {"book_receive_monotonic_ns", value.book_receive_ns},
        {"mark_receive_monotonic_ns", value.mark_receive_ns}, {"perp_bid", value.bid},
        {"perp_ask", value.ask}, {"perp_mid", 0.5 * (value.bid + value.ask)},
        {"perp_microprice", value.microprice}, {"perp_bid_depth_l20", value.bid_depth_l20},
        {"perp_ask_depth_l20", value.ask_depth_l20}, {"perp_trade_imbalance", value.signed_trade_flow},
        {"mark_price", value.mark_price}, {"index_price", value.index_price}, {"funding_rate", value.funding_rate},
        {"liquidation_buy_notional", value.liquidation_buy_notional},
        {"liquidation_sell_notional", value.liquidation_sell_notional},
        {"frames", value.frames}, {"parse_failures", value.parse_failures},
        {"depth_frames", value.depth_frames}, {"trade_frames", value.trade_frames},
        {"mark_frames", value.mark_frames}, {"liquidation_frames", value.liquidation_frames},
        {"ignored_frames", value.ignored_frames},
        {"open_interest", nullptr}, {"open_interest_state", "PENDING_RATE_AWARE_REST"},
    };
}

struct DeribitMetrics {
    std::int64_t receive_ns = 0;
    double best_bid = 0.0;
    double best_ask = 0.0;
    double mark_price = 0.0;
    double index_price = 0.0;
    double current_funding = 0.0;
    double funding_8h = 0.0;
    double open_interest = 0.0;
    double signed_trade_flow = 0.0;
    std::uint64_t ticker_frames = 0;
    std::uint64_t trade_frames = 0;
    std::uint64_t parse_failures = 0;
    std::uint8_t valid = 0;
};

json::object deribit_json(const DeribitMetrics& value) {
    return {
        {"valid", value.valid != 0}, {"receive_monotonic_ns", value.receive_ns},
        {"best_bid", value.best_bid}, {"best_ask", value.best_ask},
        {"mark_price", value.mark_price}, {"index_price", value.index_price},
        {"current_funding", value.current_funding}, {"funding_8h", value.funding_8h},
        {"open_interest", value.open_interest}, {"signed_trade_flow", value.signed_trade_flow},
        {"ticker_frames", value.ticker_frames}, {"trade_frames", value.trade_frames},
        {"parse_failures", value.parse_failures},
    };
}

class BinanceUsdMObserver final : public ExternalFrameObserver {
public:
    void on_connection_epoch(std::uint64_t) noexcept override {}

    void on_frame(std::uint64_t, std::int64_t receive_ns, std::int64_t,
                  std::string_view payload) noexcept override {
        try {
            boost::system::error_code error;
            const auto raw = json::parse(payload, error);
            if (error || !raw.is_object()) { fail(); return; }
            const json::object* root = &raw.as_object();
            if (const auto* data = json_field(*root, "data"); data != nullptr && data->is_object()) root = &data->as_object();
            const auto* event = json_field(*root, "e");
            std::lock_guard lock(mutex_);
            ++metrics_.frames;
            if (json_text_equals(event, "depthUpdate")) { ++metrics_.depth_frames; update_depth(*root, receive_ns); }
            else if (json_text_equals(event, "aggTrade")) { ++metrics_.trade_frames; update_trade(*root); }
            else if (json_text_equals(event, "markPriceUpdate")) { ++metrics_.mark_frames; update_mark(*root, receive_ns); }
            else if (json_text_equals(event, "forceOrder")) { ++metrics_.liquidation_frames; update_liquidation(*root); }
            else ++metrics_.ignored_frames;
        } catch (...) { fail(); }
    }

    [[nodiscard]] BinanceUsdMMetrics metrics() const noexcept {
        std::lock_guard lock(mutex_);
        return metrics_;
    }

private:
    void fail() noexcept { std::lock_guard lock(mutex_); ++metrics_.parse_failures; }

    void update_depth(const json::object& root, std::int64_t receive_ns) noexcept {
        const auto* bids = json_field(root, "b");
        const auto* asks = json_field(root, "a");
        if (bids == nullptr || asks == nullptr || !bids->is_array() || !asks->is_array()
            || bids->as_array().empty() || asks->as_array().empty()) { ++metrics_.parse_failures; return; }
        auto total = [](const json::array& levels, double& first_price, double& first_quantity, double& sum) noexcept {
            for (std::size_t index = 0; index < levels.size(); ++index) {
                if (!levels[index].is_array() || levels[index].as_array().size() < 2) return false;
                const auto& level = levels[index].as_array();
                double price = 0.0, quantity = 0.0;
                if (!json_number(&level[0], price) || !json_number(&level[1], quantity) || price <= 0.0 || quantity < 0.0) return false;
                if (index == 0) { first_price = price; first_quantity = quantity; }
                sum += quantity;
            }
            return true;
        };
        double bid = 0.0, ask = 0.0, bid_l1 = 0.0, ask_l1 = 0.0, bid_sum = 0.0, ask_sum = 0.0;
        if (!total(bids->as_array(), bid, bid_l1, bid_sum) || !total(asks->as_array(), ask, ask_l1, ask_sum) || bid >= ask) {
            ++metrics_.parse_failures; return;
        }
        metrics_.bid = bid; metrics_.ask = ask;
        metrics_.bid_depth_l20 = bid_sum; metrics_.ask_depth_l20 = ask_sum;
        metrics_.microprice = (bid_l1 + ask_l1) > 0.0 ? (ask * bid_l1 + bid * ask_l1) / (bid_l1 + ask_l1) : 0.5 * (bid + ask);
        metrics_.book_receive_ns = receive_ns; metrics_.valid = 1;
    }

    void update_trade(const json::object& root) noexcept {
        double quantity = 0.0;
        bool buyer_is_maker = false;
        if (!json_number(json_field(root, "q"), quantity) || !json_bool(json_field(root, "m"), buyer_is_maker) || quantity <= 0.0) {
            ++metrics_.parse_failures; return;
        }
        const double sign = buyer_is_maker ? -1.0 : 1.0;
        metrics_.signed_trade_flow = 0.9 * metrics_.signed_trade_flow + 0.1 * sign * quantity;
    }

    void update_mark(const json::object& root, std::int64_t receive_ns) noexcept {
        double mark = 0.0, index = 0.0, funding = 0.0;
        if (!json_number(json_field(root, "p"), mark) || !json_number(json_field(root, "i"), index)
            || !json_number(json_field(root, "r"), funding) || mark <= 0.0 || index <= 0.0) { ++metrics_.parse_failures; return; }
        metrics_.mark_price = mark; metrics_.index_price = index; metrics_.funding_rate = funding; metrics_.mark_receive_ns = receive_ns;
    }

    void update_liquidation(const json::object& root) noexcept {
        const auto* order = json_field(root, "o");
        if (order == nullptr || !order->is_object()) { ++metrics_.parse_failures; return; }
        double price = 0.0, quantity = 0.0;
        if (!json_number(json_field(order->as_object(), "ap"), price) || !json_number(json_field(order->as_object(), "q"), quantity)
            || price <= 0.0 || quantity <= 0.0) { ++metrics_.parse_failures; return; }
        if (json_text_equals(json_field(order->as_object(), "S"), "BUY")) metrics_.liquidation_buy_notional += price * quantity;
        else if (json_text_equals(json_field(order->as_object(), "S"), "SELL")) metrics_.liquidation_sell_notional += price * quantity;
        else ++metrics_.parse_failures;
    }

    mutable std::mutex mutex_{};
    BinanceUsdMMetrics metrics_{};
};

class DeribitObserver final : public ExternalFrameObserver {
public:
    void on_connection_epoch(std::uint64_t) noexcept override {
        std::lock_guard lock(mutex_);
        metrics_.valid = 0;
    }

    void on_frame(std::uint64_t, std::int64_t receive_ns, std::int64_t,
                  std::string_view payload) noexcept override {
        try {
            boost::system::error_code error;
            const auto raw = json::parse(payload, error);
            if (error || !raw.is_object()) { fail(); return; }
            const auto& root = raw.as_object();
            if (!json_text_equals(json_field(root, "method"), "subscription")) return;
            const auto* params = json_field(root, "params");
            if (params == nullptr || !params->is_object()) { fail(); return; }
            const auto& parameter = params->as_object();
            const auto* channel = json_field(parameter, "channel");
            const auto* data = json_field(parameter, "data");
            if (channel == nullptr || !channel->is_string() || data == nullptr) { fail(); return; }
            const std::string_view channel_text(channel->as_string().data(), channel->as_string().size());
            std::lock_guard lock(mutex_);
            if (channel_text.starts_with("ticker.BTC-PERPETUAL.") && data->is_object()) {
                update_ticker(data->as_object(), receive_ns);
            } else if (channel_text.starts_with("trades.BTC-PERPETUAL.") && data->is_array()) {
                update_trades(data->as_array());
            }
        } catch (...) { fail(); }
    }

    [[nodiscard]] DeribitMetrics metrics() const noexcept {
        std::lock_guard lock(mutex_);
        return metrics_;
    }

private:
    void fail() noexcept { std::lock_guard lock(mutex_); ++metrics_.parse_failures; metrics_.valid = 0; }

    void update_ticker(const json::object& ticker, std::int64_t receive_ns) noexcept {
        double bid = 0.0, ask = 0.0, mark = 0.0, index = 0.0, current_funding = 0.0, funding_8h = 0.0, open_interest = 0.0;
        if (!json_number(json_field(ticker, "best_bid_price"), bid)
            || !json_number(json_field(ticker, "best_ask_price"), ask)
            || !json_number(json_field(ticker, "mark_price"), mark)
            || !json_number(json_field(ticker, "index_price"), index)
            || !json_number(json_field(ticker, "current_funding"), current_funding)
            || !json_number(json_field(ticker, "funding_8h"), funding_8h)
            || !json_number(json_field(ticker, "open_interest"), open_interest)
            || bid <= 0.0 || ask <= bid || mark <= 0.0 || index <= 0.0 || open_interest < 0.0) {
            ++metrics_.parse_failures;
            metrics_.valid = 0;
            return;
        }
        metrics_.best_bid = bid; metrics_.best_ask = ask; metrics_.mark_price = mark;
        metrics_.index_price = index; metrics_.current_funding = current_funding;
        metrics_.funding_8h = funding_8h; metrics_.open_interest = open_interest;
        metrics_.receive_ns = receive_ns; ++metrics_.ticker_frames; metrics_.valid = 1;
    }

    void update_trades(const json::array& trades) noexcept {
        ++metrics_.trade_frames;
        for (const auto& raw_trade : trades) {
            if (!raw_trade.is_object()) { ++metrics_.parse_failures; continue; }
            const auto& trade = raw_trade.as_object();
            double amount = 0.0;
            if (!json_number(json_field(trade, "amount"), amount) || amount <= 0.0) { ++metrics_.parse_failures; continue; }
            if (json_text_equals(json_field(trade, "direction"), "buy")) metrics_.signed_trade_flow = .9 * metrics_.signed_trade_flow + .1 * amount;
            else if (json_text_equals(json_field(trade, "direction"), "sell")) metrics_.signed_trade_flow = .9 * metrics_.signed_trade_flow - .1 * amount;
            else ++metrics_.parse_failures;
        }
    }

    mutable std::mutex mutex_{};
    DeribitMetrics metrics_{};
};

class BinanceSpotL2Observer final : public ExternalFrameObserver {
public:
    void on_connection_epoch(std::uint64_t epoch) noexcept override {
        std::lock_guard lock(mutex_);
        book_.begin_recovery();
        connection_epoch_ = epoch;
    }

    void on_frame(std::uint64_t epoch, std::int64_t receive_ns, std::int64_t,
                  std::string_view payload) noexcept override {
        BinanceDepthDelta delta;
        const auto parsed = parse_binance_spot_depth_delta(payload, receive_ns, delta);
        if (parsed == BinanceDepthParseState::Ignored) return;
        std::lock_guard lock(mutex_);
        if (epoch != connection_epoch_) {
            book_.begin_recovery();
            connection_epoch_ = epoch;
        }
        poll_snapshot();
        if (parsed != BinanceDepthParseState::Parsed || !book_.buffer_delta(std::move(delta))) {
            book_.begin_recovery();
        }
        start_snapshot_if_needed();
    }

    [[nodiscard]] BinanceL2Metrics metrics() const noexcept {
        std::lock_guard lock(mutex_);
        return book_.metrics();
    }

private:
    static std::optional<BinanceDepthSnapshot> fetch_snapshot() noexcept {
        try {
            net::io_context io;
            ssl::context context(ssl::context::tls_client);
            context.set_default_verify_paths();
            context.set_verify_mode(ssl::verify_peer);
            tcp::resolver resolver(io);
            beast::ssl_stream<beast::tcp_stream> stream(io, context);
            const auto endpoints = resolver.resolve("api.binance.com", "443");
            beast::get_lowest_layer(stream).connect(endpoints);
            if (!SSL_set_tlsext_host_name(stream.native_handle(), "api.binance.com")) return std::nullopt;
            stream.handshake(ssl::stream_base::client);
            http::request<http::empty_body> request{http::verb::get, "/api/v3/depth?symbol=BTCUSDT&limit=1000", 11};
            request.set(http::field::host, "api.binance.com");
            request.set(http::field::user_agent, "polymarket-v7-external-l2/1");
            http::write(stream, request);
            beast::flat_buffer buffer;
            http::response<http::string_body> response;
            http::read(stream, buffer, response);
            const auto receive_ns = monotonic_now_ns();
            boost::system::error_code ignored;
            stream.shutdown(ignored);
            BinanceDepthSnapshot output;
            if (response.result() != http::status::ok
                || parse_binance_depth_snapshot(response.body(), receive_ns, output) != BinanceDepthParseState::Parsed) {
                return std::nullopt;
            }
            return output;
        } catch (...) {
            return std::nullopt;
        }
    }

    void poll_snapshot() noexcept {
        if (!snapshot_future_.valid()
            || snapshot_future_.wait_for(std::chrono::seconds(0)) != std::future_status::ready) return;
        const auto completed_epoch = snapshot_epoch_;
        const auto snapshot = snapshot_future_.get();
        if (completed_epoch != connection_epoch_ || !snapshot || !book_.install_snapshot(*snapshot)) {
            book_.begin_recovery();
        }
    }

    void start_snapshot_if_needed() noexcept {
        if (book_.state() != BinanceL2State::Buffering || snapshot_future_.valid()) return;
        snapshot_epoch_ = connection_epoch_;
        try {
            snapshot_future_ = std::async(std::launch::async, [] { return fetch_snapshot(); });
        } catch (...) {
            book_.begin_recovery();
        }
    }

    mutable std::mutex mutex_{};
    BinanceL2Book book_{};
    std::future<std::optional<BinanceDepthSnapshot>> snapshot_future_{};
    std::uint64_t connection_epoch_ = 0;
    std::uint64_t snapshot_epoch_ = 0;
};

class CoinbaseL2Observer final : public ExternalFrameObserver {
public:
    CoinbaseL2Observer(ExternalVenueIngress& ingress, std::uint64_t asset_handle) noexcept
        : ingress_(ingress), asset_handle_(asset_handle) {}

    void on_connection_epoch(std::uint64_t epoch) noexcept override {
        std::lock_guard lock(mutex_);
        book_.begin_recovery();
        connection_epoch_ = epoch;
        sequence_ = 0;
        ExternalVenueEvent event;
        event.asset_handle = asset_handle_;
        event.connection_epoch = epoch;
        event.source_sequence = ++sequence_;
        event.venue = VenueId::CoinbaseSpot;
        event.event_type = ExternalEventType::Health;
        event.local_receive_monotonic_ns = monotonic_now_ns();
        event.local_receive_wall_ns = wall_now_ns();
        event.healthy = 0;
        (void)ingress_.on_event(event);
    }

    void on_frame(std::uint64_t epoch, std::int64_t receive_ns, std::int64_t wall_ns,
                  std::string_view payload) noexcept override {
        try {
            boost::system::error_code error;
            const auto raw = json::parse(payload, error);
            if (error || !raw.is_object()) { fail("invalid_json"); return; }
            const auto& root = raw.as_object();
            const auto* type = json_field(root, "type");
            if (type != nullptr && type->is_string()) {
                std::lock_guard lock(mutex_);
                const auto& text = type->as_string();
                last_message_type_.assign(text.data(), std::min<std::size_t>(text.size(), 64));
            }
            if (json_text_equals(type, "snapshot")) {
                CoinbaseDepthSnapshot snapshot;
                snapshot.local_receive_monotonic_ns = receive_ns;
                if (!parse_levels(json_field(root, "bids"), snapshot.bids)
                    || !parse_levels(json_field(root, "asks"), snapshot.asks)) { fail("snapshot_shape"); return; }
                std::lock_guard lock(mutex_);
                if (epoch != connection_epoch_) reset_epoch(epoch);
                if (!book_.install_snapshot(snapshot)) { ++parse_failures_; last_protocol_error_ = "snapshot_invalid"; return; }
                publish(receive_ns, wall_ns);
                return;
            }
            if (json_text_equals(type, "l2update")) {
                CoinbaseDepthUpdate update;
                update.local_receive_monotonic_ns = receive_ns;
                if (!parse_changes(json_field(root, "changes"), update.changes)) { fail("l2update_shape"); return; }
                std::lock_guard lock(mutex_);
                if (epoch != connection_epoch_) reset_epoch(epoch);
                if (!book_.apply_update(update)) { ++parse_failures_; last_protocol_error_ = "l2update_invalid"; return; }
                publish(receive_ns, wall_ns);
                return;
            }
            if (json_text_equals(type, "error")) {
                const auto* message = json_field(root, "message");
                std::string detail = "exchange_error";
                if (message != nullptr && message->is_string()) {
                    const auto& text = message->as_string();
                    detail.append(":").append(text.data(), std::min<std::size_t>(text.size(), 160));
                }
                fail(std::move(detail));
                return;
            }
            // Subscription confirmations, heartbeat, and matches carry no
            // complete book transition and therefore cannot alter L2 state.
        } catch (...) { fail("exception"); }
    }

    [[nodiscard]] CoinbaseL2Metrics metrics() const noexcept {
        std::lock_guard lock(mutex_);
        auto out = book_.metrics();
        out.parse_failures = parse_failures_;
        return out;
    }

    [[nodiscard]] std::string diagnostic() const {
        std::lock_guard lock(mutex_);
        return "last_type=" + last_message_type_ + ";" + last_protocol_error_;
    }

private:
    static bool parse_levels(const json::value* raw, std::vector<CoinbaseDepthLevel>& output) noexcept {
        if (raw == nullptr || !raw->is_array() || raw->as_array().empty()) return false;
        try {
            output.reserve(raw->as_array().size());
            for (const auto& level_raw : raw->as_array()) {
                if (!level_raw.is_array() || level_raw.as_array().size() != 2) return false;
                const auto& level = level_raw.as_array();
                CoinbaseDepthLevel parsed;
                if (!json_number(&level[0], parsed.price) || !json_number(&level[1], parsed.quantity)
                    || parsed.price <= 0.0 || parsed.quantity <= 0.0) return false;
                output.push_back(parsed);
            }
        } catch (...) { return false; }
        return true;
    }

    static bool parse_changes(const json::value* raw, std::vector<CoinbaseDepthChange>& output) noexcept {
        if (raw == nullptr || !raw->is_array() || raw->as_array().empty()) return false;
        try {
            output.reserve(raw->as_array().size());
            for (const auto& change_raw : raw->as_array()) {
                if (!change_raw.is_array() || change_raw.as_array().size() != 3) return false;
                const auto& change = change_raw.as_array();
                CoinbaseDepthChange parsed;
                if (json_text_equals(&change[0], "buy")) parsed.bid = true;
                else if (json_text_equals(&change[0], "sell")) parsed.bid = false;
                else return false;
                if (!json_number(&change[1], parsed.price) || !json_number(&change[2], parsed.quantity)
                    || parsed.price <= 0.0 || parsed.quantity < 0.0) return false;
                output.push_back(parsed);
            }
        } catch (...) { return false; }
        return true;
    }

    void reset_epoch(std::uint64_t epoch) noexcept {
        book_.begin_recovery();
        connection_epoch_ = epoch;
        sequence_ = 0;
    }

    void publish(std::int64_t receive_ns, std::int64_t wall_ns) noexcept {
        const auto current = book_.metrics();
        if (current.valid == 0) return;
        ExternalVenueEvent event;
        event.asset_handle = asset_handle_;
        event.connection_epoch = connection_epoch_;
        event.source_sequence = ++sequence_;
        event.venue = VenueId::CoinbaseSpot;
        event.event_type = ExternalEventType::BookTop;
        event.local_receive_monotonic_ns = receive_ns;
        event.local_receive_wall_ns = wall_ns;
        event.bid = current.best_bid;
        event.ask = current.best_ask;
        event.bid_size = current.bid_depth_l1;
        event.ask_size = current.ask_depth_l1;
        event.healthy = 1;
        (void)ingress_.on_event(event);
    }

    void fail(std::string diagnostic) noexcept {
        std::lock_guard lock(mutex_);
        ++parse_failures_;
        last_protocol_error_ = std::move(diagnostic);
        book_.begin_recovery();
    }

    ExternalVenueIngress& ingress_;
    std::uint64_t asset_handle_ = 0;
    mutable std::mutex mutex_{};
    CoinbaseL2Book book_{};
    std::uint64_t connection_epoch_ = 0;
    std::uint64_t sequence_ = 0;
    std::uint64_t parse_failures_ = 0;
    std::string last_protocol_error_{};
    std::string last_message_type_{};
};

class BybitL2Observer final : public ExternalFrameObserver {
public:
    BybitL2Observer(ExternalVenueIngress& ingress, std::uint64_t asset_handle,
                    VenueId venue = VenueId::BybitSpot) noexcept
        : ingress_(ingress), asset_handle_(asset_handle), venue_(venue) {}

    void on_connection_epoch(std::uint64_t epoch) noexcept override {
        std::lock_guard lock(mutex_);
        reset_epoch(epoch);
    }

    void on_frame(std::uint64_t epoch, std::int64_t receive_ns, std::int64_t wall_ns,
                  std::string_view payload) noexcept override {
        try {
            boost::system::error_code error;
            const auto raw = json::parse(payload, error);
            if (error || !raw.is_object()) { fail(); return; }
            const auto& root = raw.as_object();
            const auto* topic = json_field(root, "topic");
            if (topic == nullptr || !topic->is_string()
                || !std::string_view(topic->as_string().data(), topic->as_string().size()).starts_with("orderbook.50.BTCUSDT")) return;
            const auto* type = json_field(root, "type");
            const auto* data = json_field(root, "data");
            if (type == nullptr || data == nullptr || !data->is_object()) { fail(); return; }
            const auto& book_data = data->as_object();
            std::uint64_t update_id = 0;
            if (!json_u64(json_field(book_data, "u"), update_id) || update_id == 0) { fail(); return; }
            if (json_text_equals(type, "snapshot")) {
                CoinbaseDepthSnapshot snapshot;
                snapshot.local_receive_monotonic_ns = receive_ns;
                if (!parse_levels(json_field(book_data, "b"), false, snapshot.bids)
                    || !parse_levels(json_field(book_data, "a"), false, snapshot.asks)) { fail(); return; }
                std::lock_guard lock(mutex_);
                if (epoch != connection_epoch_) reset_epoch(epoch);
                if (!book_.install_snapshot(snapshot)) { ++parse_failures_; return; }
                last_update_id_ = update_id;
                publish(receive_ns, wall_ns, update_id);
                return;
            }
            if (!json_text_equals(type, "delta")) return;
            CoinbaseDepthUpdate update;
            update.local_receive_monotonic_ns = receive_ns;
            if (!parse_levels(json_field(book_data, "b"), true, update.changes, true)
                || !parse_levels(json_field(book_data, "a"), true, update.changes, false)
                || update.changes.empty()) { fail(); return; }
            std::lock_guard lock(mutex_);
            if (epoch != connection_epoch_ || update_id <= last_update_id_ || !book_.apply_update(update)) {
                ++parse_failures_;
                book_.begin_recovery();
                return;
            }
            last_update_id_ = update_id;
            publish(receive_ns, wall_ns, update_id);
        } catch (...) { fail(); }
    }

    [[nodiscard]] CoinbaseL2Metrics metrics() const noexcept {
        std::lock_guard lock(mutex_);
        auto out = book_.metrics();
        out.parse_failures = parse_failures_;
        return out;
    }

private:
    static bool parse_levels(const json::value* raw, bool allow_empty,
                             std::vector<CoinbaseDepthLevel>& output) noexcept {
        if (raw == nullptr || !raw->is_array() || (!allow_empty && raw->as_array().empty())) return false;
        try {
            output.reserve(output.size() + raw->as_array().size());
            for (const auto& level_raw : raw->as_array()) {
                if (!level_raw.is_array() || level_raw.as_array().size() < 2) return false;
                const auto& level = level_raw.as_array();
                CoinbaseDepthLevel parsed;
                if (!json_number(&level[0], parsed.price) || !json_number(&level[1], parsed.quantity)
                    || parsed.price <= 0.0 || parsed.quantity < 0.0 || (!allow_empty && parsed.quantity == 0.0)) return false;
                output.push_back(parsed);
            }
        } catch (...) { return false; }
        return true;
    }

    static bool parse_levels(const json::value* raw, bool allow_empty,
                             std::vector<CoinbaseDepthChange>& output, bool bid) noexcept {
        if (raw == nullptr || !raw->is_array()) return false;
        try {
            output.reserve(output.size() + raw->as_array().size());
            for (const auto& level_raw : raw->as_array()) {
                if (!level_raw.is_array() || level_raw.as_array().size() < 2) return false;
                const auto& level = level_raw.as_array();
                CoinbaseDepthChange parsed;
                parsed.bid = bid;
                if (!json_number(&level[0], parsed.price) || !json_number(&level[1], parsed.quantity)
                    || parsed.price <= 0.0 || parsed.quantity < 0.0 || (!allow_empty && parsed.quantity == 0.0)) return false;
                output.push_back(parsed);
            }
        } catch (...) { return false; }
        return true;
    }

    void reset_epoch(std::uint64_t epoch) noexcept {
        book_.begin_recovery();
        connection_epoch_ = epoch;
        last_update_id_ = 0;
    }

    void publish(std::int64_t receive_ns, std::int64_t wall_ns, std::uint64_t update_id) noexcept {
        const auto current = book_.metrics();
        if (current.valid == 0) return;
        ExternalVenueEvent event;
        event.asset_handle = asset_handle_;
        event.connection_epoch = connection_epoch_;
        event.source_sequence = update_id;
        event.venue = venue_;
        event.event_type = ExternalEventType::BookTop;
        event.local_receive_monotonic_ns = receive_ns;
        event.local_receive_wall_ns = wall_ns;
        event.bid = current.best_bid;
        event.ask = current.best_ask;
        event.bid_size = current.bid_depth_l1;
        event.ask_size = current.ask_depth_l1;
        event.healthy = 1;
        (void)ingress_.on_event(event);
    }

    void fail() noexcept {
        std::lock_guard lock(mutex_);
        ++parse_failures_;
        book_.begin_recovery();
        last_update_id_ = 0;
    }

    ExternalVenueIngress& ingress_;
    std::uint64_t asset_handle_ = 0;
    VenueId venue_ = VenueId::BybitSpot;
    mutable std::mutex mutex_{};
    CoinbaseL2Book book_{};
    std::uint64_t connection_epoch_ = 0;
    std::uint64_t last_update_id_ = 0;
    std::uint64_t parse_failures_ = 0;
};

struct BybitLinearMarketMetrics {
    std::int64_t ticker_receive_ns = 0;
    double mark_price = 0.0;
    double index_price = 0.0;
    double funding_rate = 0.0;
    double open_interest = 0.0;
    double signed_trade_flow = 0.0;
    double liquidation_buy_notional = 0.0;
    double liquidation_sell_notional = 0.0;
    std::uint64_t ticker_frames = 0;
    std::uint64_t trade_frames = 0;
    std::uint64_t liquidation_frames = 0;
    std::uint64_t parse_failures = 0;
    std::uint8_t valid = 0;
};

json::object bybit_linear_json(const BybitLinearMarketMetrics& value) {
    return {
        {"valid", value.valid != 0}, {"ticker_receive_monotonic_ns", value.ticker_receive_ns},
        {"mark_price", value.mark_price}, {"index_price", value.index_price},
        {"funding_rate", value.funding_rate}, {"open_interest", value.open_interest},
        {"signed_trade_flow", value.signed_trade_flow},
        {"liquidation_buy_notional", value.liquidation_buy_notional},
        {"liquidation_sell_notional", value.liquidation_sell_notional},
        {"ticker_frames", value.ticker_frames}, {"trade_frames", value.trade_frames},
        {"liquidation_frames", value.liquidation_frames}, {"parse_failures", value.parse_failures},
    };
}

class BybitLinearMarketObserver final : public ExternalFrameObserver {
public:
    void on_connection_epoch(std::uint64_t) noexcept override {
        std::lock_guard lock(mutex_);
        metrics_.valid = 0;
    }

    void on_frame(std::uint64_t, std::int64_t receive_ns, std::int64_t,
                  std::string_view payload) noexcept override {
        try {
            boost::system::error_code error;
            const auto raw = json::parse(payload, error);
            if (error || !raw.is_object()) { fail(); return; }
            const auto& root = raw.as_object();
            const auto* topic = json_field(root, "topic");
            const auto* data = json_field(root, "data");
            if (topic == nullptr || !topic->is_string() || data == nullptr) return;
            const std::string_view channel(topic->as_string().data(), topic->as_string().size());
            std::lock_guard lock(mutex_);
            if (channel == "tickers.BTCUSDT" && data->is_object()) update_ticker(data->as_object(), receive_ns);
            else if (channel == "publicTrade.BTCUSDT" && data->is_array()) update_trades(data->as_array());
            else if (channel == "allLiquidation.BTCUSDT" && data->is_object()) update_liquidation(data->as_object());
        } catch (...) { fail(); }
    }

    [[nodiscard]] BybitLinearMarketMetrics metrics() const noexcept {
        std::lock_guard lock(mutex_);
        return metrics_;
    }

private:
    void fail() noexcept { std::lock_guard lock(mutex_); ++metrics_.parse_failures; metrics_.valid = 0; }

    bool update_optional(const json::object& data, std::string_view key, double& target,
                         bool nonnegative = false) noexcept {
        const auto* value = json_field(data, key);
        if (value == nullptr) return true;
        double parsed = 0.0;
        if (!json_number(value, parsed) || (!nonnegative && !std::isfinite(parsed))
            || (nonnegative && parsed < 0.0)) return false;
        target = parsed;
        return true;
    }

    void update_ticker(const json::object& data, std::int64_t receive_ns) noexcept {
        if (!update_optional(data, "markPrice", metrics_.mark_price)
            || !update_optional(data, "indexPrice", metrics_.index_price)
            || !update_optional(data, "fundingRate", metrics_.funding_rate)
            || !update_optional(data, "openInterest", metrics_.open_interest, true)) {
            ++metrics_.parse_failures; metrics_.valid = 0; return;
        }
        ++metrics_.ticker_frames;
        metrics_.ticker_receive_ns = receive_ns;
        metrics_.valid = metrics_.mark_price > 0.0 && metrics_.index_price > 0.0 ? 1 : 0;
    }

    void update_trades(const json::array& trades) noexcept {
        ++metrics_.trade_frames;
        for (const auto& raw_trade : trades) {
            if (!raw_trade.is_object()) { ++metrics_.parse_failures; continue; }
            const auto& trade = raw_trade.as_object();
            double size = 0.0;
            if (!json_number(json_field(trade, "v"), size) || size <= 0.0) { ++metrics_.parse_failures; continue; }
            if (json_text_equals(json_field(trade, "S"), "Buy")) metrics_.signed_trade_flow = .9 * metrics_.signed_trade_flow + .1 * size;
            else if (json_text_equals(json_field(trade, "S"), "Sell")) metrics_.signed_trade_flow = .9 * metrics_.signed_trade_flow - .1 * size;
            else ++metrics_.parse_failures;
        }
    }

    void update_liquidation(const json::object& value) noexcept {
        ++metrics_.liquidation_frames;
        double price = 0.0, size = 0.0;
        if (!json_number(json_field(value, "p"), price) || !json_number(json_field(value, "v"), size)
            || price <= 0.0 || size <= 0.0) { ++metrics_.parse_failures; return; }
        if (json_text_equals(json_field(value, "S"), "Buy")) metrics_.liquidation_buy_notional += price * size;
        else if (json_text_equals(json_field(value, "S"), "Sell")) metrics_.liquidation_sell_notional += price * size;
        else ++metrics_.parse_failures;
    }

    mutable std::mutex mutex_{};
    BybitLinearMarketMetrics metrics_{};
};

class FanoutObserver final : public ExternalFrameObserver {
public:
    FanoutObserver(ExternalFrameObserver& first, ExternalFrameObserver& second) noexcept
        : first_(first), second_(second) {}
    void on_connection_epoch(std::uint64_t epoch) noexcept override {
        first_.on_connection_epoch(epoch);
        second_.on_connection_epoch(epoch);
    }
    void on_frame(std::uint64_t epoch, std::int64_t receive_ns, std::int64_t wall_ns,
                  std::string_view payload) noexcept override {
        first_.on_frame(epoch, receive_ns, wall_ns, payload);
        second_.on_frame(epoch, receive_ns, wall_ns, payload);
    }
private:
    ExternalFrameObserver& first_;
    ExternalFrameObserver& second_;
};
} // namespace

int main(int argc, char** argv) {
    try {
        fs::path output;
        fs::path tape_path;
        fs::path raw_tape_dir;
        fs::path normalized_event_tape_dir;
        std::string model_sha;
        for (int index = 1; index < argc; ++index) {
            const std::string argument = argv[index];
            if (argument == "--output" && index + 1 < argc) output = argv[++index];
            else if (argument == "--tape" && index + 1 < argc) tape_path = argv[++index];
            else if (argument == "--raw-tape-dir" && index + 1 < argc) raw_tape_dir = argv[++index];
            else if (argument == "--normalized-event-tape-dir" && index + 1 < argc) normalized_event_tape_dir = argv[++index];
            else if (argument == "--model-sha" && index + 1 < argc) model_sha = argv[++index];
            else throw std::invalid_argument("unknown or incomplete argument: " + argument);
        }
        if (output.empty() || model_sha.size() != 40) {
            throw std::invalid_argument("--output and exact --model-sha are required");
        }
        std::signal(SIGINT, signal_handler);
        std::signal(SIGTERM, signal_handler);

        constexpr std::uint64_t asset_handle = 0x425443555344ULL; // BTCUSD
        std::unique_ptr<ExternalTapeRecorder> normalized_tape;
        if (!tape_path.empty()) {
            const auto session = "external-venues-" + std::to_string(::getpid());
            normalized_tape = std::make_unique<ExternalTapeRecorder>(
                tape_path, model_sha, "paper-v7", session, "external-venue-runtime", wall_now_ns());
        }
        const auto normalized_event_tape = [&](std::string source) {
            if (normalized_event_tape_dir.empty()) return std::unique_ptr<ExternalTapeRecorder>{};
            const auto session = "external-events-" + std::to_string(::getpid());
            return std::make_unique<ExternalTapeRecorder>(
                normalized_event_tape_dir / (source + "." + std::to_string(::getpid()) + ".bin"),
                model_sha, "paper-v7", session, std::move(source), wall_now_ns());
        };
        auto binance_event_tape = normalized_event_tape("binance-spot");
        auto coinbase_event_tape = normalized_event_tape("coinbase-spot");
        auto bybit_event_tape = normalized_event_tape("bybit-spot");
        auto bybit_linear_event_tape = normalized_event_tape("bybit-linear");
        auto deribit_event_tape = normalized_event_tape("deribit");
        const auto raw_tape = [&](std::string source) {
            if (raw_tape_dir.empty()) return std::unique_ptr<ExternalRawTapeRecorder>{};
            const auto session = "external-raw-" + std::to_string(::getpid());
            return std::make_unique<ExternalRawTapeRecorder>(
                raw_tape_dir / (source + "." + std::to_string(::getpid()) + ".bin"),
                model_sha, "paper-v7", session, std::move(source), wall_now_ns());
        };
        auto binance_raw_tape = raw_tape("binance-spot");
        auto coinbase_raw_tape = raw_tape("coinbase-spot");
        auto bybit_raw_tape = raw_tape("bybit-spot");
        auto bybit_linear_raw_tape = raw_tape("bybit-linear");
        auto deribit_raw_tape = raw_tape("deribit");
        auto binance_usdm_depth_raw_tape = raw_tape("binance-usdm-depth");
        auto binance_usdm_market_raw_tape = raw_tape("binance-usdm-market");
        std::uint64_t tape_sequence = 0;
        ExternalStatePolicy policy;
        ExternalAssetState state(asset_handle);
        ExternalVenueIngress binance_ingress(VenueId::BinanceSpot, asset_handle, binance_event_tape.get());
        ExternalVenueIngress coinbase_ingress(VenueId::CoinbaseSpot, asset_handle, coinbase_event_tape.get());
        ExternalVenueIngress bybit_ingress(VenueId::BybitSpot, asset_handle, bybit_event_tape.get());
        ExternalVenueIngress bybit_linear_ingress(VenueId::BybitLinear, asset_handle, bybit_linear_event_tape.get());
        ExternalVenueIngress deribit_ingress(VenueId::Deribit, asset_handle, deribit_event_tape.get());
        BinanceSpotL2Observer binance_l2;
        CoinbaseL2Observer coinbase_l2(coinbase_ingress, asset_handle);
        BybitL2Observer bybit_l2(bybit_ingress, asset_handle);
        BybitL2Observer bybit_linear_l2(bybit_linear_ingress, asset_handle, VenueId::BybitLinear);
        BybitLinearMarketObserver bybit_linear_market;
        FanoutObserver bybit_linear_observer(bybit_linear_l2, bybit_linear_market);
        BinanceUsdMObserver binance_usdm_observer;
        DeribitObserver deribit_observer;
        ExternalVenueWsClient binance(btc_spot_connection_spec(VenueId::BinanceSpot, asset_handle), &binance_ingress, &binance_l2, binance_raw_tape.get());
        ExternalVenueWsClient coinbase(btc_spot_connection_spec(VenueId::CoinbaseSpot, asset_handle), nullptr, &coinbase_l2, coinbase_raw_tape.get());
        ExternalVenueWsClient bybit(btc_spot_connection_spec(VenueId::BybitSpot, asset_handle), &bybit_ingress, &bybit_l2, bybit_raw_tape.get());
        ExternalVenueWsClient bybit_linear(btc_spot_connection_spec(VenueId::BybitLinear, asset_handle), nullptr, &bybit_linear_observer, bybit_linear_raw_tape.get());
        ExternalVenueWsClient deribit(btc_spot_connection_spec(VenueId::Deribit, asset_handle), &deribit_ingress, &deribit_observer, deribit_raw_tape.get());
        auto binance_usdm_depth_spec = btc_spot_connection_spec(VenueId::BinanceUsdM, asset_handle);
        binance_usdm_depth_spec.subscription_json =
            R"({"method":"SUBSCRIBE","params":["btcusdt@depth20@100ms"],"id":1})";
        auto binance_usdm_market_spec = btc_spot_connection_spec(VenueId::BinanceUsdM, asset_handle);
        binance_usdm_market_spec.subscription_json =
            R"({"method":"SUBSCRIBE","params":["btcusdt@aggTrade","btcusdt@markPrice@1s","btcusdt@forceOrder"],"id":2})";
        ExternalVenueWsClient binance_usdm_depth(std::move(binance_usdm_depth_spec), nullptr, &binance_usdm_observer, binance_usdm_depth_raw_tape.get());
        ExternalVenueWsClient binance_usdm_market(std::move(binance_usdm_market_spec), nullptr, &binance_usdm_observer, binance_usdm_market_raw_tape.get());

#if defined(__APPLE__)
        std::thread binance_thread([&] { binance.run(ExternalStopToken(stopping)); });
        std::thread coinbase_thread([&] { coinbase.run(ExternalStopToken(stopping)); });
        std::thread bybit_thread([&] { bybit.run(ExternalStopToken(stopping)); });
        std::thread bybit_linear_thread([&] { bybit_linear.run(ExternalStopToken(stopping)); });
        std::thread deribit_thread([&] { deribit.run(ExternalStopToken(stopping)); });
        std::thread binance_usdm_depth_thread([&] { binance_usdm_depth.run(ExternalStopToken(stopping)); });
        std::thread binance_usdm_market_thread([&] { binance_usdm_market.run(ExternalStopToken(stopping)); });
#else
        std::jthread binance_thread([&](std::stop_token token) { binance.run(token); });
        std::jthread coinbase_thread([&](std::stop_token token) { coinbase.run(token); });
        std::jthread bybit_thread([&](std::stop_token token) { bybit.run(token); });
        std::jthread bybit_linear_thread([&](std::stop_token token) { bybit_linear.run(token); });
        std::jthread deribit_thread([&](std::stop_token token) { deribit.run(token); });
        std::jthread binance_usdm_depth_thread([&](std::stop_token token) { binance_usdm_depth.run(token); });
        std::jthread binance_usdm_market_thread([&](std::stop_token token) { binance_usdm_market.run(token); });
#endif

        while (!stopping.load(std::memory_order_relaxed)) {
            const auto drained = binance_ingress.drain_into(state, policy)
                + coinbase_ingress.drain_into(state, policy)
                + bybit_ingress.drain_into(state, policy)
                + bybit_linear_ingress.drain_into(state, policy)
                + deribit_ingress.drain_into(state, policy);
            const auto now_mono = monotonic_now_ns();
            const auto snapshot = state.snapshot(now_mono, policy);
            TapeRecorderSnapshot tape_status;
            if (normalized_tape != nullptr) {
                (void)normalized_tape->try_record(make_tape_record(
                    TapeRecordKind::ExternalAssetSnapshot, ++tape_sequence,
                    now_mono, asset_handle, snapshot));
                tape_status = normalized_tape->snapshot();
            }
            const auto binance_status = binance.snapshot();
            const auto coinbase_status = coinbase.snapshot();
            const auto bybit_status = bybit.snapshot();
            const auto bybit_linear_status = bybit_linear.snapshot();
            const auto deribit_status = deribit.snapshot();
            const auto binance_usdm_depth_status = binance_usdm_depth.snapshot();
            const auto binance_usdm_market_status = binance_usdm_market.snapshot();
            json::array venues;
            venues.emplace_back(transport_json(binance_status, "BINANCE_SPOT"));
            venues.emplace_back(transport_json(coinbase_status, "COINBASE_SPOT"));
            venues.emplace_back(transport_json(bybit_status, "BYBIT_SPOT"));
            venues.emplace_back(transport_json(bybit_linear_status, "BYBIT_LINEAR"));
            venues.emplace_back(transport_json(deribit_status, "DERIBIT"));
            venues.emplace_back(transport_json(binance_usdm_depth_status, "BINANCE_USDM_DEPTH"));
            venues.emplace_back(transport_json(binance_usdm_market_status, "BINANCE_USDM_MARKET"));
            atomic_write(output, {
                {"schema", "polymarket_v7_external_venue_runtime_v1"},
                {"timestamp_ns", wall_now_ns()},
                {"code_sha", model_sha},
                {"paper_only", true},
                {"authenticated_execution", false},
                {"real_order_submission", false},
                {"state", snapshot.valid != 0 ? "OPERATIONAL" : "WARMING_OR_DEGRADED"},
                {"valid", snapshot.valid != 0},
                {"state_version", snapshot.state_version},
                {"composite_price", snapshot.venue_composite_price},
                {"composite_microprice", snapshot.venue_composite_microprice},
                {"dispersion_bps", snapshot.venue_dispersion_bps},
                {"fresh_venue_count", snapshot.venue_count_fresh},
                {"venue_health_mask", snapshot.venue_health_mask},
                {"return_250ms", snapshot.venue_composite_return_250ms},
                {"return_1s", snapshot.venue_composite_return_1s},
                {"return_5s", snapshot.venue_composite_return_5s},
                {"realized_vol_fast", snapshot.realized_vol_fast},
                {"realized_vol_medium", snapshot.realized_vol_medium},
                {"realized_vol_slow", snapshot.realized_vol_slow},
                {"aggregate_ofi", snapshot.aggregate_ofi},
                {"aggregate_trade_imbalance", snapshot.aggregate_trade_imbalance},
                {"latest_input_receive_monotonic_ns", snapshot.latest_input_receive_monotonic_ns},
                {"drained_last_cycle", drained},
                {"normalized_snapshot_tape", tape_json(tape_status, normalized_tape != nullptr)},
                {"normalized_event_tapes", {
                    {"binance_spot", tape_json(binance_event_tape ? binance_event_tape->snapshot() : TapeRecorderSnapshot{}, binance_event_tape != nullptr)},
                    {"coinbase_spot", tape_json(coinbase_event_tape ? coinbase_event_tape->snapshot() : TapeRecorderSnapshot{}, coinbase_event_tape != nullptr)},
                    {"bybit_spot", tape_json(bybit_event_tape ? bybit_event_tape->snapshot() : TapeRecorderSnapshot{}, bybit_event_tape != nullptr)},
                    {"bybit_linear", tape_json(bybit_linear_event_tape ? bybit_linear_event_tape->snapshot() : TapeRecorderSnapshot{}, bybit_linear_event_tape != nullptr)},
                    {"deribit", tape_json(deribit_event_tape ? deribit_event_tape->snapshot() : TapeRecorderSnapshot{}, deribit_event_tape != nullptr)},
                }},
                {"raw_frame_tapes", {
                    {"binance_spot", tape_json(binance_raw_tape ? binance_raw_tape->snapshot() : TapeRecorderSnapshot{}, binance_raw_tape != nullptr)},
                    {"coinbase_spot", tape_json(coinbase_raw_tape ? coinbase_raw_tape->snapshot() : TapeRecorderSnapshot{}, coinbase_raw_tape != nullptr)},
                    {"bybit_spot", tape_json(bybit_raw_tape ? bybit_raw_tape->snapshot() : TapeRecorderSnapshot{}, bybit_raw_tape != nullptr)},
                    {"bybit_linear", tape_json(bybit_linear_raw_tape ? bybit_linear_raw_tape->snapshot() : TapeRecorderSnapshot{}, bybit_linear_raw_tape != nullptr)},
                    {"deribit", tape_json(deribit_raw_tape ? deribit_raw_tape->snapshot() : TapeRecorderSnapshot{}, deribit_raw_tape != nullptr)},
                    {"binance_usdm_depth", tape_json(binance_usdm_depth_raw_tape ? binance_usdm_depth_raw_tape->snapshot() : TapeRecorderSnapshot{}, binance_usdm_depth_raw_tape != nullptr)},
                    {"binance_usdm_market", tape_json(binance_usdm_market_raw_tape ? binance_usdm_market_raw_tape->snapshot() : TapeRecorderSnapshot{}, binance_usdm_market_raw_tape != nullptr)},
                }},
                {"binance_spot_l2", l2_json(binance_l2.metrics())},
                {"coinbase_spot_l2", l2_json(coinbase_l2.metrics())},
                {"coinbase_spot_l2_diagnostic", coinbase_l2.diagnostic()},
                {"bybit_spot_l2", l2_json(bybit_l2.metrics())},
                {"bybit_linear_l2", l2_json(bybit_linear_l2.metrics())},
                {"bybit_linear", bybit_linear_json(bybit_linear_market.metrics())},
                {"deribit", deribit_json(deribit_observer.metrics())},
                {"binance_usdm", usdm_json(binance_usdm_observer.metrics())},
                {"venues", std::move(venues)},
            });
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }

#if !defined(__APPLE__)
        binance_thread.request_stop();
        coinbase_thread.request_stop();
        bybit_thread.request_stop();
        bybit_linear_thread.request_stop();
        deribit_thread.request_stop();
        binance_usdm_depth_thread.request_stop();
        binance_usdm_market_thread.request_stop();
#endif
        if (binance_thread.joinable()) binance_thread.join();
        if (coinbase_thread.joinable()) coinbase_thread.join();
        if (bybit_thread.joinable()) bybit_thread.join();
        if (bybit_linear_thread.joinable()) bybit_linear_thread.join();
        if (deribit_thread.joinable()) deribit_thread.join();
        if (binance_usdm_depth_thread.joinable()) binance_usdm_depth_thread.join();
        if (binance_usdm_market_thread.joinable()) binance_usdm_market_thread.join();
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "v7_external_venue_runtime: " << error.what() << '\n';
        return 2;
    }
}
