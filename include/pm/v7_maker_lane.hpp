#pragma once

#include "pm/v7_maker_hft.hpp"
#include "pm/v7_market_ws.hpp"

#include <cstdint>

namespace pm::v7::maker {

struct MakerLaneContext {
    InventorySnapshot inventory{};
    QuoteSnapshot quotes{};
    RiskSnapshot risk{};
    double conservative_rebate_ev_per_share = 0.0;
    double conservative_reward_ev_per_share = 0.0;
    double related_fair_value = 0.0;
    std::int64_t related_snapshot_age_ns = 0;
    std::uint8_t related_state_valid = 0;
};

// One lane owns the incremental MakerHotPath state for exactly one token. It
// consumes the canonical V7 market-data event; it never owns another book.
// inventory_sign is +1 for YES and -1 for the complementary NO token.
class MakerInstrumentLane final {
public:
    explicit MakerInstrumentLane(std::int8_t inventory_sign) noexcept;

    [[nodiscard]] MakerDecision on_market_event(
        const MarketWsEvent& event,
        const MakerLaneContext& context,
        const MakerModelSnapshot& model) noexcept;

    [[nodiscard]] std::int8_t inventory_sign() const noexcept { return inventory_sign_; }

private:
    [[nodiscard]] InventorySnapshot oriented_inventory(
        const InventorySnapshot& inventory) const noexcept;

    MakerHotPath hot_{};
    double previous_bid_depth_l5_ = 0.0;
    double previous_ask_depth_l5_ = 0.0;
    double pending_trade_bid_removal_ = 0.0;
    double pending_trade_ask_removal_ = 0.0;
    std::int8_t inventory_sign_ = 1;
    std::uint8_t book_initialized_ = 0;
};

} // namespace pm::v7::maker
