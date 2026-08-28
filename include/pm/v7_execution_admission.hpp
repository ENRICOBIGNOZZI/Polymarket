#pragma once

#include "pm/v7_capital.hpp"
#include "pm/v7_intent.hpp"

#include <cstdint>

namespace pm::v7 {

enum class ExecutionAdmissionReason : std::uint8_t {
    Accepted = 1,
    ControlBypass = 2,
    InvalidIntent = 3,
    InvalidTick = 4,
    CapitalDenied = 5,
    UnsupportedIntent = 6,
};

struct ExecutionAdmissionResult {
    ExecutionAdmissionReason reason = ExecutionAdmissionReason::InvalidIntent;
    std::int64_t reserved_microdollars = 0;
    std::uint8_t accepted = 0;
    std::uint8_t capital_reserved = 0;
};

// Common, deterministic pre-trade admission primitive for already-planned
// single-leg intents. The intended owner is the single order-TX/arbitration
// thread. It performs no I/O, no allocation and no model inference.
//
// CANCEL/WITHDRAW/KILL bypass new-capital admission. Passive BUY and aggressive
// target BUY both reserve their worst-case limit notional. SELL remains cash
// neutral here and must independently pass canonical inventory reservation.
class ExecutionAdmission final {
public:
    [[nodiscard]] static ExecutionAdmissionResult admit(
        const StrategyIntent& intent,
        std::int32_t tick_size_e4,
        SleeveCapitalAccount& capital) noexcept;

    [[nodiscard]] static bool quote_buy_notional_microdollars(
        const StrategyIntent& intent,
        std::int32_t tick_size_e4,
        std::int64_t& out_microdollars) noexcept;

    [[nodiscard]] static bool aggressive_buy_notional_microdollars(
        const StrategyIntent& intent,
        std::int32_t tick_size_e4,
        std::int64_t& out_microdollars) noexcept;
};

} // namespace pm::v7
