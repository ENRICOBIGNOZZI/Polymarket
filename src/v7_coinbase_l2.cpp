#include "pm/v7_coinbase_l2.hpp"

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

bool CoinbaseL2Book::valid_levels(const std::vector<CoinbaseDepthLevel>& levels) noexcept {
    return !levels.empty() && std::all_of(levels.begin(), levels.end(), [](const auto& level) {
        return finite_positive(level.price) && finite_positive(level.quantity);
    });
}

bool CoinbaseL2Book::valid_changes(const std::vector<CoinbaseDepthChange>& changes) noexcept {
    return !changes.empty() && std::all_of(changes.begin(), changes.end(), [](const auto& change) {
        return finite_positive(change.price) && std::isfinite(change.quantity) && change.quantity >= 0.0;
    });
}

void CoinbaseL2Book::replace_levels(Bids& target, const std::vector<CoinbaseDepthLevel>& levels) {
    target.clear();
    for (const auto& level : levels) target[level.price] = level.quantity;
}

void CoinbaseL2Book::replace_levels(Asks& target, const std::vector<CoinbaseDepthLevel>& levels) {
    target.clear();
    for (const auto& level : levels) target[level.price] = level.quantity;
}

void CoinbaseL2Book::apply_changes(Bids& target, const std::vector<CoinbaseDepthChange>& changes) {
    for (const auto& change : changes) {
        if (!change.bid) continue;
        if (change.quantity == 0.0) target.erase(change.price);
        else target[change.price] = change.quantity;
    }
}

void CoinbaseL2Book::apply_changes(Asks& target, const std::vector<CoinbaseDepthChange>& changes) {
    for (const auto& change : changes) {
        if (change.bid) continue;
        if (change.quantity == 0.0) target.erase(change.price);
        else target[change.price] = change.quantity;
    }
}

bool CoinbaseL2Book::uncrossed() const noexcept {
    return !bids_.empty() && !asks_.empty() && bids_.begin()->first < asks_.begin()->first;
}

void CoinbaseL2Book::gap() noexcept {
    bids_.clear();
    asks_.clear();
    latest_receive_monotonic_ns_ = 0;
    state_ = CoinbaseL2State::Gapped;
}

void CoinbaseL2Book::begin_recovery() noexcept {
    bids_.clear();
    asks_.clear();
    update_count_ = 0;
    latest_receive_monotonic_ns_ = 0;
    state_ = CoinbaseL2State::AwaitingSnapshot;
}

bool CoinbaseL2Book::install_snapshot(const CoinbaseDepthSnapshot& snapshot) {
    if (snapshot.local_receive_monotonic_ns <= 0 || !valid_levels(snapshot.bids) || !valid_levels(snapshot.asks)) {
        gap();
        return false;
    }
    replace_levels(bids_, snapshot.bids);
    replace_levels(asks_, snapshot.asks);
    if (!uncrossed()) {
        gap();
        return false;
    }
    update_count_ = 0;
    latest_receive_monotonic_ns_ = snapshot.local_receive_monotonic_ns;
    state_ = CoinbaseL2State::Live;
    return true;
}

bool CoinbaseL2Book::apply_update(const CoinbaseDepthUpdate& update) {
    if (state_ != CoinbaseL2State::Live || update.local_receive_monotonic_ns <= 0 || !valid_changes(update.changes)) {
        gap();
        return false;
    }
    apply_changes(bids_, update.changes);
    apply_changes(asks_, update.changes);
    if (!uncrossed()) {
        gap();
        return false;
    }
    ++update_count_;
    latest_receive_monotonic_ns_ = update.local_receive_monotonic_ns;
    return true;
}

CoinbaseL2Metrics CoinbaseL2Book::metrics() const noexcept {
    CoinbaseL2Metrics out;
    out.update_count = update_count_;
    out.latest_receive_monotonic_ns = latest_receive_monotonic_ns_;
    out.state = state_;
    out.bid_levels = static_cast<std::uint32_t>(std::min<std::size_t>(bids_.size(), std::numeric_limits<std::uint32_t>::max()));
    out.ask_levels = static_cast<std::uint32_t>(std::min<std::size_t>(asks_.size(), std::numeric_limits<std::uint32_t>::max()));
    if (state_ != CoinbaseL2State::Live || !uncrossed()) return out;
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
