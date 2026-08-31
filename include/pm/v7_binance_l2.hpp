#pragma once

#include <cstddef>
#include <cstdint>
#include <map>
#include <vector>

namespace pm::v7::external_fair {

// Binance public diff-depth reconstruction. This deliberately has no OMS,
// model, or execution dependency: it is a single-writer market-data state
// machine fed by buffered WebSocket deltas and bounded REST snapshots.
struct BinanceDepthLevel {
    double price = 0.0;
    double quantity = 0.0;
};

struct BinanceDepthDelta {
    std::uint64_t first_update_id = 0; // U
    std::uint64_t final_update_id = 0; // u
    std::int64_t source_event_ns = 0;
    std::int64_t local_receive_monotonic_ns = 0;
    std::vector<BinanceDepthLevel> bids;
    std::vector<BinanceDepthLevel> asks;
};

struct BinanceDepthSnapshot {
    std::uint64_t last_update_id = 0;
    std::int64_t local_receive_monotonic_ns = 0;
    std::vector<BinanceDepthLevel> bids;
    std::vector<BinanceDepthLevel> asks;
};

enum class BinanceL2State : std::uint8_t {
    Buffering = 1,
    Live = 2,
    Gapped = 3,
};

struct BinanceL2Metrics {
    std::uint64_t last_update_id = 0;
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
    BinanceL2State state = BinanceL2State::Buffering;
    std::uint8_t valid = 0;
};

class BinanceL2Book final {
public:
    explicit BinanceL2Book(std::size_t max_buffered_deltas = 4096);

    // Invoke before requesting a REST snapshot. A buffer overflow is a gap: no
    // partial depth state is retained or exposed as valid.
    [[nodiscard]] bool buffer_delta(BinanceDepthDelta delta);
    [[nodiscard]] bool install_snapshot(const BinanceDepthSnapshot& snapshot);
    [[nodiscard]] bool apply_live_delta(const BinanceDepthDelta& delta);
    void begin_recovery() noexcept;

    [[nodiscard]] BinanceL2State state() const noexcept { return state_; }
    [[nodiscard]] BinanceL2Metrics metrics() const noexcept;

private:
    using Bids = std::map<double, double, std::greater<double>>;
    using Asks = std::map<double, double, std::less<double>>;

    [[nodiscard]] static bool valid_levels(const std::vector<BinanceDepthLevel>& levels) noexcept;
    static void replace_levels(Bids& target, const std::vector<BinanceDepthLevel>& levels);
    static void replace_levels(Asks& target, const std::vector<BinanceDepthLevel>& levels);
    static void apply_levels(Bids& target, const std::vector<BinanceDepthLevel>& levels);
    static void apply_levels(Asks& target, const std::vector<BinanceDepthLevel>& levels);
    [[nodiscard]] bool apply_checked(const BinanceDepthDelta& delta, bool bridge);
    [[nodiscard]] bool uncrossed() const noexcept;

    std::size_t max_buffered_deltas_ = 0;
    Bids bids_{};
    Asks asks_{};
    std::vector<BinanceDepthDelta> buffered_{};
    std::uint64_t last_update_id_ = 0;
    std::int64_t latest_receive_monotonic_ns_ = 0;
    BinanceL2State state_ = BinanceL2State::Buffering;
};

} // namespace pm::v7::external_fair
