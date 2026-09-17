#include "pm/v7_coinbase_l2_observer.hpp"

#include <boost/json.hpp>

#include <algorithm>
#include <charconv>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <string>
#include <vector>

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
    try {
        if (raw->is_double()) out = raw->as_double();
        else if (raw->is_int64()) out = static_cast<double>(raw->as_int64());
        else if (raw->is_uint64()) out = static_cast<double>(raw->as_uint64());
        else if (raw->is_string()) {
            const auto& s = raw->as_string();
            std::string text(s.data(), s.size());
            char* end = nullptr;
            out = std::strtod(text.c_str(), &end);
            if (end == text.c_str() || *end != '\0') return false;
        } else return false;
        return std::isfinite(out);
    } catch (...) { return false; }
}
bool parse_levels(const json::value* raw, std::vector<CoinbaseDepthLevel>& output) noexcept {
    if (raw == nullptr || !raw->is_array() || raw->as_array().empty()) return false;
    try {
        output.reserve(raw->as_array().size());
        for (const auto& row_raw : raw->as_array()) {
            if (!row_raw.is_array() || row_raw.as_array().size() != 2) return false;
            const auto& row = row_raw.as_array();
            CoinbaseDepthLevel parsed;
            if (!number(&row[0], parsed.price) || !number(&row[1], parsed.quantity)
                || parsed.price <= 0.0 || parsed.quantity <= 0.0) return false;
            output.push_back(parsed);
        }
    } catch (...) { return false; }
    return true;
}
bool parse_changes(const json::value* raw, std::vector<CoinbaseDepthChange>& output) noexcept {
    if (raw == nullptr || !raw->is_array() || raw->as_array().empty()) return false;
    try {
        output.reserve(raw->as_array().size());
        for (const auto& row_raw : raw->as_array()) {
            if (!row_raw.is_array() || row_raw.as_array().size() != 3) return false;
            const auto& row = row_raw.as_array();
            CoinbaseDepthChange parsed;
            if (text_equals(&row[0], "buy")) parsed.bid = true;
            else if (text_equals(&row[0], "sell")) parsed.bid = false;
            else return false;
            if (!number(&row[1], parsed.price) || !number(&row[2], parsed.quantity)
                || parsed.price <= 0.0 || parsed.quantity < 0.0) return false;
            output.push_back(parsed);
        }
    } catch (...) { return false; }
    return true;
}

} // namespace

CoinbaseL2FrameObserver::CoinbaseL2FrameObserver(
    ExternalVenueIngress& ingress, std::uint64_t asset_handle) noexcept
    : ingress_(ingress), asset_handle_(asset_handle) {}

void CoinbaseL2FrameObserver::on_connection_epoch(std::uint64_t epoch) noexcept {
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

void CoinbaseL2FrameObserver::on_frame(
    std::uint64_t epoch, std::int64_t receive_ns,
    std::int64_t wall_ns, std::string_view payload) noexcept {
    try {
        boost::system::error_code error;
        const auto raw = json::parse(payload, error);
        if (error || !raw.is_object()) { fail("invalid_json"); return; }
        const auto& root = raw.as_object();
        const auto* type = field(root, "type");
        if (type != nullptr && type->is_string()) {
            std::lock_guard lock(mutex_);
            const auto& text = type->as_string();
            last_message_type_.assign(text.data(), std::min<std::size_t>(text.size(), 64));
        }
        if (text_equals(type, "snapshot")) {
            CoinbaseDepthSnapshot snapshot;
            snapshot.local_receive_monotonic_ns = receive_ns;
            if (!parse_levels(field(root, "bids"), snapshot.bids)
                || !parse_levels(field(root, "asks"), snapshot.asks)) {
                fail("snapshot_shape"); return;
            }
            std::lock_guard lock(mutex_);
            if (epoch != connection_epoch_) reset_epoch(epoch);
            if (!book_.install_snapshot(snapshot)) {
                ++parse_failures_; last_protocol_error_ = "snapshot_invalid"; return;
            }
            publish(receive_ns, wall_ns);
            return;
        }
        if (text_equals(type, "l2update")) {
            CoinbaseDepthUpdate update;
            update.local_receive_monotonic_ns = receive_ns;
            if (!parse_changes(field(root, "changes"), update.changes)) {
                fail("l2update_shape"); return;
            }
            std::lock_guard lock(mutex_);
            if (epoch != connection_epoch_) reset_epoch(epoch);
            if (!book_.apply_update(update)) {
                ++parse_failures_; last_protocol_error_ = "l2update_invalid"; return;
            }
            publish(receive_ns, wall_ns);
            return;
        }
        if (text_equals(type, "error")) {
            const auto* message = field(root, "message");
            std::string detail = "exchange_error";
            if (message != nullptr && message->is_string()) {
                const auto& text = message->as_string();
                detail.append(":").append(text.data(), std::min<std::size_t>(text.size(), 160));
            }
            fail(std::move(detail));
        }
    } catch (...) { fail("exception"); }
}

CoinbaseL2Metrics CoinbaseL2FrameObserver::metrics() const noexcept {
    std::lock_guard lock(mutex_);
    auto out = book_.metrics();
    out.parse_failures = parse_failures_;
    return out;
}

std::string CoinbaseL2FrameObserver::diagnostic() const {
    std::lock_guard lock(mutex_);
    return "last_type=" + last_message_type_ + ";" + last_protocol_error_;
}

void CoinbaseL2FrameObserver::reset_epoch(std::uint64_t epoch) noexcept {
    book_.begin_recovery();
    connection_epoch_ = epoch;
    sequence_ = 0;
}

void CoinbaseL2FrameObserver::publish(std::int64_t receive_ns, std::int64_t wall_ns) noexcept {
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

void CoinbaseL2FrameObserver::fail(std::string diagnostic) noexcept {
    std::lock_guard lock(mutex_);
    ++parse_failures_;
    last_protocol_error_ = std::move(diagnostic);
    book_.begin_recovery();
}

} // namespace pm::v7::external_fair
