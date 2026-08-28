#include "pm/v7_execution_admission.hpp"

#include <limits>

namespace pm::v7 {
namespace {

[[nodiscard]] bool mul_div_ceil_positive(std::int64_t lhs,
                                         std::int64_t rhs,
                                         std::int64_t divisor,
                                         std::int64_t& out) noexcept {
    if (lhs <= 0 || rhs <= 0 || divisor <= 0) return false;
    const std::int64_t whole = lhs / divisor;
    const std::int64_t remainder = lhs % divisor;
    if (whole > std::numeric_limits<std::int64_t>::max() / rhs) return false;
    const std::int64_t base = whole * rhs;
    const std::int64_t rem_product = remainder * rhs;
    const std::int64_t tail = rem_product == 0
        ? 0 : 1 + (rem_product - 1) / divisor;
    if (base > std::numeric_limits<std::int64_t>::max() - tail) return false;
    out = base + tail;
    return out > 0;
}

[[nodiscard]] bool valid_common_identity(const StrategyIntent& intent) noexcept {
    return intent.intent_id != 0
        && intent.market_handle != 0
        && intent.instrument_handle != 0
        && intent.quantity_microunits > 0
        && intent.price_tick > 0
        && (intent.side == Side::Buy || intent.side == Side::Sell);
}

[[nodiscard]] bool valid_quote_identity(const StrategyIntent& intent) noexcept {
    return valid_common_identity(intent)
        && intent.type == IntentType::Quote
        && intent.passive != 0
        && intent.post_only != 0;
}

[[nodiscard]] bool valid_aggressive_identity(const StrategyIntent& intent) noexcept {
    return valid_common_identity(intent)
        && intent.type == IntentType::TargetPosition
        && intent.passive == 0
        && intent.post_only == 0
        && (intent.urgency == Urgency::Aggressive || intent.urgency == Urgency::Critical);
}

[[nodiscard]] bool buy_notional_microdollars(
    const StrategyIntent& intent,
    std::int32_t tick_size_e4,
    std::int64_t& out_microdollars) noexcept {

    out_microdollars = 0;
    if (intent.side != Side::Buy || tick_size_e4 <= 0 || !valid_common_identity(intent)) {
        return false;
    }

    constexpr std::int64_t kPriceScale = 10'000;
    const auto tick = static_cast<std::int64_t>(tick_size_e4);
    if (tick >= kPriceScale) return false;
    if (intent.price_tick > (kPriceScale - 1) / tick) return false;
    const std::int64_t price_e4 = intent.price_tick * tick;
    if (price_e4 <= 0 || price_e4 >= kPriceScale) return false;

    return mul_div_ceil_positive(
        intent.quantity_microunits, price_e4, kPriceScale, out_microdollars);
}

} // namespace

bool ExecutionAdmission::quote_buy_notional_microdollars(
    const StrategyIntent& intent,
    std::int32_t tick_size_e4,
    std::int64_t& out_microdollars) noexcept {
    if (!valid_quote_identity(intent)) {
        out_microdollars = 0;
        return false;
    }
    return buy_notional_microdollars(intent, tick_size_e4, out_microdollars);
}

bool ExecutionAdmission::aggressive_buy_notional_microdollars(
    const StrategyIntent& intent,
    std::int32_t tick_size_e4,
    std::int64_t& out_microdollars) noexcept {
    if (!valid_aggressive_identity(intent)) {
        out_microdollars = 0;
        return false;
    }
    return buy_notional_microdollars(intent, tick_size_e4, out_microdollars);
}

ExecutionAdmissionResult ExecutionAdmission::admit(
    const StrategyIntent& intent,
    std::int32_t tick_size_e4,
    SleeveCapitalAccount& capital) noexcept {

    ExecutionAdmissionResult result;

    // Only non-order-producing control traffic bypasses capital. A Critical
    // aggressive target still represents new execution and must never inherit
    // this bypass merely from its urgency flag.
    if (is_critical_intent(intent.type)) {
        result.reason = ExecutionAdmissionReason::ControlBypass;
        result.accepted = 1;
        return result;
    }

    const bool passive_quote = intent.type == IntentType::Quote;
    const bool aggressive_target = intent.type == IntentType::TargetPosition;
    if (!passive_quote && !aggressive_target) {
        result.reason = ExecutionAdmissionReason::UnsupportedIntent;
        return result;
    }
    if ((passive_quote && !valid_quote_identity(intent))
        || (aggressive_target && !valid_aggressive_identity(intent))) {
        result.reason = ExecutionAdmissionReason::InvalidIntent;
        return result;
    }
    if (tick_size_e4 <= 0) {
        result.reason = ExecutionAdmissionReason::InvalidTick;
        return result;
    }

    if (intent.side == Side::Sell) {
        result.reason = ExecutionAdmissionReason::Accepted;
        result.accepted = 1;
        return result;
    }

    std::int64_t notional = 0;
    const bool notional_ok = passive_quote
        ? quote_buy_notional_microdollars(intent, tick_size_e4, notional)
        : aggressive_buy_notional_microdollars(intent, tick_size_e4, notional);
    if (!notional_ok) {
        result.reason = ExecutionAdmissionReason::InvalidTick;
        return result;
    }
    if (!capital.reserve_order(intent.intent_id, intent.market_handle, notional)) {
        result.reason = ExecutionAdmissionReason::CapitalDenied;
        return result;
    }

    result.reason = ExecutionAdmissionReason::Accepted;
    result.accepted = 1;
    result.capital_reserved = 1;
    result.reserved_microdollars = notional;
    return result;
}

} // namespace pm::v7
