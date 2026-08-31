#pragma once

#include "pm/v7_binance_l2.hpp"

#include <cstdint>
#include <map>
#include <vector>

namespace pm::v7::external_fair {

// Coinbase Exchange level2 sends a complete WebSocket snapshot followed by
// absolute-size updates. Unlike Binance diff-depth, its level2 feed guarantees
// delivery and does not expose a per-update sequence in this protocol. This
// book therefore becomes valid only after a full snapshot and fails closed on
// malformed, crossed, or out-of-session updates.
using CoinbaseDepthLevel = BinanceDepthLevel;

struct CoinbaseDepthSnapshot {
    std::int64_t local_receive_monotonic_ns = 0;
    std::vector<CoinbaseDepthLevel> bids;
    std::vector<CoinbaseDepthLevel> asks;
};

struct CoinbaseDepthChange {
    bool bid = false;
    double price = 0.0;
    double quantity = 0.0;
};

struct CoinbaseDepthUpdate {
    std::int64_t local_receive_monotonic_ns = 0;
    std::vector<CoinbaseDepthChange> changes;
};

enum class CoinbaseL2State : std::uint8_t {
    AwaitingSnapshot = 1,
    Live = 2,
    Gapped = 3,
};

struct CoinbaseL2Metrics {
    std::uint64_t update_count = 0;
    std::uint64_t parse_failures = 0;
    std::int64_t latest_receive_monotonic_ns = 0;
    double best_bid = 0.0;
    double best_ask = 0.0;
    double mid = 0.0;
    double microprice = 0.0;
    double spread_bps = 0.0;
    double bid_depth_l1 = 0.0;
    double ask_depth_l1 = 0.0;
    double bid_depth_l5 = 0.0;
    double ask_depth_l5 = 0.0;
    double bid_depth_l10 = 0.0;
    double ask_depth_l10 = 0.0;
    double bid_depth_l20 = 0.0;
    double ask_depth_l20 = 0.0;
    double imbalance_l1 = 0.0;
    double imbalance_l5 = 0.0;
    double imbalance_l10 = 0.0;
    std::uint32_t bid_levels = 0;
    std::uint32_t ask_levels = 0;
    CoinbaseL2State state = CoinbaseL2State::AwaitingSnapshot;
    std::uint8_t valid = 0;
};

class CoinbaseL2Book final {
public:
    [[nodiscard]] bool install_snapshot(const CoinbaseDepthSnapshot& snapshot);
    [[nodiscard]] bool apply_update(const CoinbaseDepthUpdate& update);
    void begin_recovery() noexcept;

    [[nodiscard]] CoinbaseL2State state() const noexcept { return state_; }
    [[nodiscard]] CoinbaseL2Metrics metrics() const noexcept;

private:
    using Bids = std::map<double, double, std::greater<double>>;
    using Asks = std::map<double, double, std::less<double>>;

    [[nodiscard]] static bool valid_levels(const std::vector<CoinbaseDepthLevel>& levels) noexcept;
    [[nodiscard]] static bool valid_changes(const std::vector<CoinbaseDepthChange>& changes) noexcept;
    static void replace_levels(Bids& target, const std::vector<CoinbaseDepthLevel>& levels);
    static void replace_levels(Asks& target, const std::vector<CoinbaseDepthLevel>& levels);
    static void apply_changes(Bids& target, const std::vector<CoinbaseDepthChange>& changes);
    static void apply_changes(Asks& target, const std::vector<CoinbaseDepthChange>& changes);
    [[nodiscard]] bool uncrossed() const noexcept;
    void gap() noexcept;

    Bids bids_{};
    Asks asks_{};
    std::uint64_t update_count_ = 0;
    std::int64_t latest_receive_monotonic_ns_ = 0;
    CoinbaseL2State state_ = CoinbaseL2State::AwaitingSnapshot;
};

} // namespace pm::v7::external_fair
