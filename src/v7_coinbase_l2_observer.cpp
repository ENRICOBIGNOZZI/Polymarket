#include "pm/v7_coinbase_l2_observer.hpp"

#include <boost/json.hpp>
#include <boost/json/static_resource.hpp>
#include <boost/json/parser.hpp>
#include <boost/json/null_resource.hpp>

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <span>

namespace pm::v7::external_fair {
namespace json = boost::json;
namespace {

std::int64_t monotonic_now_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}
std::int64_t wall_now_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}
const json::value* field(const json::object& object, std::string_view key) noexcept {
    const auto it = object.find(key);
    return it == object.end() ? nullptr : &it->value();
}
bool text_equals(const json::value* raw, std::string_view expected) noexcept {
    return raw != nullptr && raw->is_string() && raw->as_string() == expected;
}
bool number(const json::value* raw, double& out) noexcept {
    if (raw == nullptr) return false;
    if (raw->is_double()) out = raw->as_double();
    else if (raw->is_int64()) out = static_cast<double>(raw->as_int64());
    else if (raw->is_uint64()) out = static_cast<double>(raw->as_uint64());
    else if (raw->is_string()) {
        const auto& source = raw->as_string();
        if (source.empty() || source.size() >= 64) return false;
        std::array<char, 64> text{};
        std::memcpy(text.data(), source.data(), source.size());
        char* end = nullptr;
        out = std::strtod(text.data(), &end);
        if (end != text.data() + static_cast<std::ptrdiff_t>(source.size())) return false;
    } else return false;
    return std::isfinite(out);
}

bool parse_levels(const json::value* raw,
                  std::span<CoinbaseDepthLevel> output,
                  std::size_t& count) noexcept {
    count = 0;
    if (raw == nullptr || !raw->is_array() || raw->as_array().empty()
        || raw->as_array().size() > output.size()) return false;
    for (const auto& row_raw : raw->as_array()) {
        if (!row_raw.is_array() || row_raw.as_array().size() != 2) return false;
        const auto& row = row_raw.as_array();
        CoinbaseDepthLevel parsed;
        if (!number(&row[0], parsed.price) || !number(&row[1], parsed.quantity)
            || parsed.price <= 0.0 || parsed.quantity <= 0.0) return false;
        output[count++] = parsed;
    }
    return true;
}

bool parse_changes(const json::value* raw,
                   std::span<CoinbaseDepthChange> output,
                   std::size_t& count) noexcept {
    count = 0;
    if (raw == nullptr || !raw->is_array() || raw->as_array().empty()
        || raw->as_array().size() > output.size()) return false;
    for (const auto& row_raw : raw->as_array()) {
        if (!row_raw.is_array() || row_raw.as_array().size() != 3) return false;
        const auto& row = row_raw.as_array();
        CoinbaseDepthChange parsed;
        if (text_equals(&row[0], "buy")) parsed.bid = true;
        else if (text_equals(&row[0], "sell")) parsed.bid = false;
        else return false;
        if (!number(&row[1], parsed.price) || !number(&row[2], parsed.quantity)
            || parsed.price <= 0.0 || parsed.quantity < 0.0) return false;
        output[count++] = parsed;
    }
    return true;
}

enum class MessageTypeCode : std::uint8_t { Unknown=0, Snapshot=1, L2Update=2, Error=3, Other=4 };
enum class ProtocolErrorCode : std::uint8_t {
    None=0, InvalidJsonOrArena=1, SnapshotShapeOrCapacity=2, SnapshotInvalid=3,
    UpdateShapeOrCapacity=4, UpdateInvalid=5, ExchangeError=6,
    BoundedArenaExhausted=7, Exception=8
};

const char* message_type_text(std::uint8_t code) noexcept {
    switch (static_cast<MessageTypeCode>(code)) {
        case MessageTypeCode::Snapshot: return "snapshot";
        case MessageTypeCode::L2Update: return "l2update";
        case MessageTypeCode::Error: return "error";
        case MessageTypeCode::Other: return "other";
        default: return "unknown";
    }
}
const char* protocol_error_text(std::uint8_t code) noexcept {
    switch (static_cast<ProtocolErrorCode>(code)) {
        case ProtocolErrorCode::InvalidJsonOrArena: return "invalid_json_or_arena";
        case ProtocolErrorCode::SnapshotShapeOrCapacity: return "snapshot_shape_or_capacity";
        case ProtocolErrorCode::SnapshotInvalid: return "snapshot_invalid";
        case ProtocolErrorCode::UpdateShapeOrCapacity: return "l2update_shape_or_capacity";
        case ProtocolErrorCode::UpdateInvalid: return "l2update_invalid";
        case ProtocolErrorCode::ExchangeError: return "exchange_error";
        case ProtocolErrorCode::BoundedArenaExhausted: return "bounded_json_arena_exhausted";
        case ProtocolErrorCode::Exception: return "exception";
        default: return "";
    }
}
} // namespace

CoinbaseL2FrameObserver::CoinbaseL2FrameObserver(
    ExternalVenueIngress& ingress, std::uint64_t asset_handle)
    : ingress_(ingress), asset_handle_(asset_handle),
      json_arena_(kCoinbaseJsonArenaBytes),
      json_resource_(std::make_unique<json::static_resource>(json_arena_.data(), json_arena_.size())),
      parser_(std::make_unique<json::parser>(
          json::storage_ptr(json::get_null_resource()), json::parse_options{},
          parser_scratch_.data(), parser_scratch_.size())),
      bid_scratch_(std::make_unique<std::array<CoinbaseDepthLevel, kCoinbaseL2MaxLevelsPerSide>>()),
      ask_scratch_(std::make_unique<std::array<CoinbaseDepthLevel, kCoinbaseL2MaxLevelsPerSide>>()),
      change_scratch_(std::make_unique<std::array<CoinbaseDepthChange, kCoinbaseMaxChangesPerFrame>>()) {}

CoinbaseL2FrameObserver::~CoinbaseL2FrameObserver() = default;

void CoinbaseL2FrameObserver::on_connection_epoch(std::uint64_t epoch) noexcept {
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
    publish_telemetry(book_.metrics());
}

void CoinbaseL2FrameObserver::on_frame(
    std::uint64_t epoch, std::int64_t receive_ns,
    std::int64_t wall_ns, std::string_view payload) noexcept {
    try {
        json_resource_->release();
        parser_->reset(json::storage_ptr(json_resource_.get()));
        boost::system::error_code error;
        const auto consumed = parser_->write(payload.data(), payload.size(), error);
        if (error || consumed != payload.size()) {
            fail("invalid_json_or_arena"); return;
        }
        const auto raw = parser_->release();
        if (!raw.is_object()) { fail("invalid_json_or_arena"); return; }
        const auto& root = raw.as_object();
        const auto* type = field(root, "type");
        if (type != nullptr && type->is_string()) set_last_type(type->as_string());

        if (text_equals(type, "snapshot")) {
            std::size_t bid_count = 0, ask_count = 0;
            if (!parse_levels(field(root, "bids"), *bid_scratch_, bid_count)
                || !parse_levels(field(root, "asks"), *ask_scratch_, ask_count)) {
                fail("snapshot_shape_or_capacity"); return;
            }
            if (epoch != connection_epoch_) reset_epoch(epoch);
            if (!book_.install_snapshot(
                    receive_ns,
                    std::span<const CoinbaseDepthLevel>(bid_scratch_->data(), bid_count),
                    std::span<const CoinbaseDepthLevel>(ask_scratch_->data(), ask_count))) {
                ++parse_failures_; set_protocol_error("snapshot_invalid"); publish_telemetry(book_.metrics()); return;
            }
            publish(receive_ns, wall_ns);
            return;
        }
        if (text_equals(type, "l2update")) {
            std::size_t change_count = 0;
            if (!parse_changes(field(root, "changes"), *change_scratch_, change_count)) {
                fail("l2update_shape_or_capacity"); return;
            }
            if (epoch != connection_epoch_) reset_epoch(epoch);
            if (!book_.apply_update(
                    receive_ns,
                    std::span<const CoinbaseDepthChange>(change_scratch_->data(), change_count))) {
                ++parse_failures_; set_protocol_error("l2update_invalid"); publish_telemetry(book_.metrics()); return;
            }
            publish(receive_ns, wall_ns);
            return;
        }
        if (text_equals(type, "error")) {
            fail("exchange_error");
        }
    } catch (const std::bad_alloc&) {
        fail("bounded_json_arena_exhausted");
    } catch (...) {
        fail("exception");
    }
}

CoinbaseL2Metrics CoinbaseL2FrameObserver::metrics() const noexcept {
    CoinbaseL2Metrics out;
    for (;;) {
        const auto before = telemetry_version_.load(std::memory_order_acquire);
        if ((before & 1U) != 0U) continue;
        out.update_count = published_update_count_.load(std::memory_order_relaxed);
        out.parse_failures = published_parse_failures_.load(std::memory_order_relaxed);
        out.latest_receive_monotonic_ns = published_latest_receive_ns_.load(std::memory_order_relaxed);
        out.best_bid = published_best_bid_.load(std::memory_order_relaxed);
        out.best_ask = published_best_ask_.load(std::memory_order_relaxed);
        out.mid = published_mid_.load(std::memory_order_relaxed);
        out.microprice = published_microprice_.load(std::memory_order_relaxed);
        out.spread_bps = published_spread_bps_.load(std::memory_order_relaxed);
        out.bid_depth_l1 = published_bid_l1_.load(std::memory_order_relaxed);
        out.ask_depth_l1 = published_ask_l1_.load(std::memory_order_relaxed);
        out.bid_depth_l5 = published_bid_l5_.load(std::memory_order_relaxed);
        out.ask_depth_l5 = published_ask_l5_.load(std::memory_order_relaxed);
        out.bid_depth_l10 = published_bid_l10_.load(std::memory_order_relaxed);
        out.ask_depth_l10 = published_ask_l10_.load(std::memory_order_relaxed);
        out.bid_depth_l20 = published_bid_l20_.load(std::memory_order_relaxed);
        out.ask_depth_l20 = published_ask_l20_.load(std::memory_order_relaxed);
        out.imbalance_l1 = published_imbalance_l1_.load(std::memory_order_relaxed);
        out.imbalance_l5 = published_imbalance_l5_.load(std::memory_order_relaxed);
        out.imbalance_l10 = published_imbalance_l10_.load(std::memory_order_relaxed);
        out.bid_levels = published_bid_levels_.load(std::memory_order_relaxed);
        out.ask_levels = published_ask_levels_.load(std::memory_order_relaxed);
        out.state = static_cast<CoinbaseL2State>(published_state_.load(std::memory_order_relaxed));
        out.valid = published_valid_.load(std::memory_order_relaxed);
        const auto after = telemetry_version_.load(std::memory_order_acquire);
        if (before == after && (after & 1U) == 0U) return out;
    }
}

std::string CoinbaseL2FrameObserver::diagnostic() const {
    const auto type = last_message_type_code_.load(std::memory_order_acquire);
    const auto error = last_protocol_error_code_.load(std::memory_order_acquire);
    return std::string("last_type=") + message_type_text(type) + ";" + protocol_error_text(error);
}

void CoinbaseL2FrameObserver::reset_epoch(std::uint64_t epoch) noexcept {
    book_.begin_recovery();
    connection_epoch_ = epoch;
    sequence_ = 0;
}

void CoinbaseL2FrameObserver::publish(std::int64_t receive_ns, std::int64_t wall_ns) noexcept {
    const auto current = book_.metrics();
    publish_telemetry(current);
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

void CoinbaseL2FrameObserver::fail(std::string_view diagnostic) noexcept {
    ++parse_failures_;
    set_protocol_error(diagnostic);
    book_.begin_recovery();
    publish_telemetry(book_.metrics());
}

void CoinbaseL2FrameObserver::publish_telemetry(const CoinbaseL2Metrics& metrics) noexcept {
    telemetry_version_.fetch_add(1, std::memory_order_acq_rel);
    published_update_count_.store(metrics.update_count, std::memory_order_relaxed);
    published_parse_failures_.store(parse_failures_, std::memory_order_relaxed);
    published_latest_receive_ns_.store(metrics.latest_receive_monotonic_ns, std::memory_order_relaxed);
    published_best_bid_.store(metrics.best_bid, std::memory_order_relaxed);
    published_best_ask_.store(metrics.best_ask, std::memory_order_relaxed);
    published_mid_.store(metrics.mid, std::memory_order_relaxed);
    published_microprice_.store(metrics.microprice, std::memory_order_relaxed);
    published_spread_bps_.store(metrics.spread_bps, std::memory_order_relaxed);
    published_bid_l1_.store(metrics.bid_depth_l1, std::memory_order_relaxed);
    published_ask_l1_.store(metrics.ask_depth_l1, std::memory_order_relaxed);
    published_bid_l5_.store(metrics.bid_depth_l5, std::memory_order_relaxed);
    published_ask_l5_.store(metrics.ask_depth_l5, std::memory_order_relaxed);
    published_bid_l10_.store(metrics.bid_depth_l10, std::memory_order_relaxed);
    published_ask_l10_.store(metrics.ask_depth_l10, std::memory_order_relaxed);
    published_bid_l20_.store(metrics.bid_depth_l20, std::memory_order_relaxed);
    published_ask_l20_.store(metrics.ask_depth_l20, std::memory_order_relaxed);
    published_imbalance_l1_.store(metrics.imbalance_l1, std::memory_order_relaxed);
    published_imbalance_l5_.store(metrics.imbalance_l5, std::memory_order_relaxed);
    published_imbalance_l10_.store(metrics.imbalance_l10, std::memory_order_relaxed);
    published_bid_levels_.store(metrics.bid_levels, std::memory_order_relaxed);
    published_ask_levels_.store(metrics.ask_levels, std::memory_order_relaxed);
    published_state_.store(static_cast<std::uint8_t>(metrics.state), std::memory_order_relaxed);
    published_valid_.store(metrics.valid, std::memory_order_relaxed);
    telemetry_version_.fetch_add(1, std::memory_order_release);
}

void CoinbaseL2FrameObserver::set_last_type(std::string_view value) noexcept {
    MessageTypeCode code = MessageTypeCode::Other;
    if (value == "snapshot") code = MessageTypeCode::Snapshot;
    else if (value == "l2update") code = MessageTypeCode::L2Update;
    else if (value == "error") code = MessageTypeCode::Error;
    last_message_type_code_.store(static_cast<std::uint8_t>(code), std::memory_order_release);
}
void CoinbaseL2FrameObserver::set_protocol_error(std::string_view value) noexcept {
    ProtocolErrorCode code = ProtocolErrorCode::Exception;
    if (value == "invalid_json_or_arena") code = ProtocolErrorCode::InvalidJsonOrArena;
    else if (value == "snapshot_shape_or_capacity") code = ProtocolErrorCode::SnapshotShapeOrCapacity;
    else if (value == "snapshot_invalid") code = ProtocolErrorCode::SnapshotInvalid;
    else if (value == "l2update_shape_or_capacity") code = ProtocolErrorCode::UpdateShapeOrCapacity;
    else if (value == "l2update_invalid") code = ProtocolErrorCode::UpdateInvalid;
    else if (value == "exchange_error") code = ProtocolErrorCode::ExchangeError;
    else if (value == "bounded_json_arena_exhausted") code = ProtocolErrorCode::BoundedArenaExhausted;
    else if (value.empty()) code = ProtocolErrorCode::None;
    last_protocol_error_code_.store(static_cast<std::uint8_t>(code), std::memory_order_release);
}

} // namespace pm::v7::external_fair
