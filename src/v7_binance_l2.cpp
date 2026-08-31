#include "pm/v7_binance_l2.hpp"

#include <algorithm>
#include <cmath>
#include <limits>

namespace pm::v7::external_fair {
namespace {

constexpr double kEps = 1e-12;

[[nodiscard]] bool finite_positive(double value) noexcept {
    return std::isfinite(value) && value > 0.0;
}

[[nodiscard]] double level_sum(const auto& levels, std::size_t count) noexcept {
    double sum = 0.0;
    std::size_t used = 0;
    for (const auto& [_, quantity] : levels) {
        if (used++ == count) break;
        sum += quantity;
    }
    return sum;
}

[[nodiscard]] double imbalance(double bid, double ask) noexcept {
    const double total = bid + ask;
    return total > kEps ? (bid - ask) / total : 0.0;
}

} // namespace

BinanceL2Book::BinanceL2Book(std::size_t max_buffered_deltas)
    : max_buffered_deltas_(max_buffered_deltas) {
    if (max_buffered_deltas_ == 0) max_buffered_deltas_ = 1;
}

bool BinanceL2Book::valid_levels(const std::vector<BinanceDepthLevel>& levels) noexcept {
    return std::all_of(levels.begin(), levels.end(), [](const BinanceDepthLevel& level) {
        return finite_positive(level.price) && std::isfinite(level.quantity) && level.quantity >= 0.0;
    });
}

void BinanceL2Book::replace_levels(Bids& target, const std::vector<BinanceDepthLevel>& levels) {
    target.clear();
    apply_levels(target, levels);
}

void BinanceL2Book::replace_levels(Asks& target, const std::vector<BinanceDepthLevel>& levels) {
    target.clear();
    apply_levels(target, levels);
}

void BinanceL2Book::apply_levels(Bids& target, const std::vector<BinanceDepthLevel>& levels) {
    for (const auto& level : levels) {
        if (level.quantity == 0.0) target.erase(level.price);
        else target[level.price] = level.quantity;
    }
}

void BinanceL2Book::apply_levels(Asks& target, const std::vector<BinanceDepthLevel>& levels) {
    for (const auto& level : levels) {
        if (level.quantity == 0.0) target.erase(level.price);
        else target[level.price] = level.quantity;
    }
}

bool BinanceL2Book::uncrossed() const noexcept {
    return !bids_.empty() && !asks_.empty() && bids_.begin()->first < asks_.begin()->first;
}

void BinanceL2Book::begin_recovery() noexcept {
    bids_.clear();
    asks_.clear();
    buffered_.clear();
    last_update_id_ = 0;
    latest_receive_monotonic_ns_ = 0;
    state_ = BinanceL2State::Buffering;
}

bool BinanceL2Book::buffer_delta(BinanceDepthDelta delta) {
    if (state_ == BinanceL2State::Live) return apply_live_delta(delta);
    if (state_ == BinanceL2State::Gapped || delta.first_update_id == 0
        || delta.final_update_id < delta.first_update_id || delta.local_receive_monotonic_ns <= 0
        || !valid_levels(delta.bids) || !valid_levels(delta.asks)) {
        state_ = BinanceL2State::Gapped;
        return false;
    }
    if (buffered_.size() >= max_buffered_deltas_) {
        state_ = BinanceL2State::Gapped;
        return false;
    }
    buffered_.push_back(std::move(delta));
    return true;
}

bool BinanceL2Book::apply_checked(const BinanceDepthDelta& delta, bool bridge) {
    const std::uint64_t expected = last_update_id_ + 1;
    const bool valid_sequence = bridge
        ? delta.first_update_id <= expected && expected <= delta.final_update_id
        : delta.first_update_id == expected;
    if (!valid_sequence || !valid_levels(delta.bids) || !valid_levels(delta.asks)
        || delta.local_receive_monotonic_ns <= 0) {
        state_ = BinanceL2State::Gapped;
        return false;
    }
    apply_levels(bids_, delta.bids);
    apply_levels(asks_, delta.asks);
    if (!uncrossed()) {
        state_ = BinanceL2State::Gapped;
        return false;
    }
    last_update_id_ = delta.final_update_id;
    latest_receive_monotonic_ns_ = delta.local_receive_monotonic_ns;
    return true;
}

bool BinanceL2Book::install_snapshot(const BinanceDepthSnapshot& snapshot) {
    if (state_ != BinanceL2State::Buffering || snapshot.last_update_id == 0
        || snapshot.local_receive_monotonic_ns <= 0 || !valid_levels(snapshot.bids)
        || !valid_levels(snapshot.asks)) {
        state_ = BinanceL2State::Gapped;
        return false;
    }
    replace_levels(bids_, snapshot.bids);
    replace_levels(asks_, snapshot.asks);
    if (!uncrossed()) {
        state_ = BinanceL2State::Gapped;
        return false;
    }
    last_update_id_ = snapshot.last_update_id;
    latest_receive_monotonic_ns_ = snapshot.local_receive_monotonic_ns;
    buffered_.erase(std::remove_if(buffered_.begin(), buffered_.end(), [&](const auto& delta) {
        return delta.final_update_id <= last_update_id_;
    }), buffered_.end());
    const auto bridge = std::find_if(buffered_.begin(), buffered_.end(), [&](const auto& delta) {
        return delta.first_update_id <= last_update_id_ + 1 && last_update_id_ + 1 <= delta.final_update_id;
    });
    if (bridge == buffered_.end() || !apply_checked(*bridge, true)) return false;
    for (auto iter = std::next(bridge); iter != buffered_.end(); ++iter) {
        if (!apply_checked(*iter, false)) return false;
    }
    buffered_.clear();
    state_ = BinanceL2State::Live;
    return true;
}

bool BinanceL2Book::apply_live_delta(const BinanceDepthDelta& delta) {
    if (state_ != BinanceL2State::Live || !apply_checked(delta, false)) return false;
    return true;
}

BinanceL2Metrics BinanceL2Book::metrics() const noexcept {
    BinanceL2Metrics out;
    out.last_update_id = last_update_id_;
    out.latest_receive_monotonic_ns = latest_receive_monotonic_ns_;
    out.state = state_;
    out.bid_levels = static_cast<std::uint32_t>(std::min<std::size_t>(bids_.size(), std::numeric_limits<std::uint32_t>::max()));
    out.ask_levels = static_cast<std::uint32_t>(std::min<std::size_t>(asks_.size(), std::numeric_limits<std::uint32_t>::max()));
    if (state_ != BinanceL2State::Live || !uncrossed()) return out;
    out.best_bid = bids_.begin()->first;
    out.best_ask = asks_.begin()->first;
    out.mid = 0.5 * (out.best_bid + out.best_ask);
    out.bid_depth_l1 = level_sum(bids_, 1);
    out.ask_depth_l1 = level_sum(asks_, 1);
    out.bid_depth_l5 = level_sum(bids_, 5);
    out.ask_depth_l5 = level_sum(asks_, 5);
    out.bid_depth_l10 = level_sum(bids_, 10);
    out.ask_depth_l10 = level_sum(asks_, 10);
    out.bid_depth_l20 = level_sum(bids_, 20);
    out.ask_depth_l20 = level_sum(asks_, 20);
    const double l1 = out.bid_depth_l1 + out.ask_depth_l1;
    out.microprice = l1 > kEps
        ? (out.best_ask * out.bid_depth_l1 + out.best_bid * out.ask_depth_l1) / l1
        : out.mid;
    out.spread_bps = (out.best_ask - out.best_bid) / out.mid * 10'000.0;
    out.imbalance_l1 = imbalance(out.bid_depth_l1, out.ask_depth_l1);
    out.imbalance_l5 = imbalance(out.bid_depth_l5, out.ask_depth_l5);
    out.imbalance_l10 = imbalance(out.bid_depth_l10, out.ask_depth_l10);
    out.valid = 1;
    return out;
}

} // namespace pm::v7::external_fair
