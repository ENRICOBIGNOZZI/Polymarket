#pragma once

#include "pm/v7_binance_l2.hpp"

#include <cstdint>
#include <memory>
#include <span>
#include <vector>

namespace pm::v7::external_fair {

// Coinbase Exchange level2 sends a full snapshot followed by absolute-size
// updates. The hot book owns fixed-capacity storage allocated once at cold
// start; snapshot/update application itself performs no heap allocation.
using CoinbaseDepthLevel = BinanceDepthLevel;

inline constexpr std::size_t kCoinbaseL2MaxLevelsPerSide = 65'536;

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
    CoinbaseL2Book();
    ~CoinbaseL2Book();
    CoinbaseL2Book(const CoinbaseL2Book&) = delete;
    CoinbaseL2Book& operator=(const CoinbaseL2Book&) = delete;

    [[nodiscard]] bool install_snapshot(const CoinbaseDepthSnapshot& snapshot);
    [[nodiscard]] bool install_snapshot(std::int64_t receive_ns,
                                        std::span<const CoinbaseDepthLevel> bids,
                                        std::span<const CoinbaseDepthLevel> asks) noexcept;
    [[nodiscard]] bool apply_update(const CoinbaseDepthUpdate& update);
    [[nodiscard]] bool apply_update(std::int64_t receive_ns,
                                    std::span<const CoinbaseDepthChange> changes) noexcept;
    void begin_recovery() noexcept;

    [[nodiscard]] CoinbaseL2State state() const noexcept { return state_; }
    [[nodiscard]] bool top_of_book(CoinbaseDepthLevel& bid, CoinbaseDepthLevel& ask) const noexcept;
    [[nodiscard]] CoinbaseL2Metrics metrics() const noexcept;

private:
    struct Storage;
    [[nodiscard]] bool uncrossed() const noexcept;
    void gap() noexcept;

    std::unique_ptr<Storage> storage_;
    std::uint64_t update_count_ = 0;
    std::int64_t latest_receive_monotonic_ns_ = 0;
    CoinbaseL2State state_ = CoinbaseL2State::AwaitingSnapshot;
};

} // namespace pm::v7::external_fair
