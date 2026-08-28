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

    // remainder < divisor (10,000 in the caller) and price_e4 < 10,000, so
    // this product is intrinsically small after the price-domain validation.
    const std::int64_t rem_product = remainder * rhs;
    const std::int64_t tail = rem_product == 0
        ? 0 : 1 + (rem_product - 1) / divisor;
    if (base > std::numeric_limits<std::int64_t>::max() - tail) return false;
    out = base + tail;
    return out > 0;
}

[[nodiscard]] bool valid_quote_identity(const StrategyIntent& intent) noexcept {
    return intent.intent_id != 0
        && intent.market_handle != 0
        && intent.instrument_handle != 0
        && intent.quantity_microunits > 0
        && intent.price_tick > 0
        && (intent.side == Side::Buy || intent.side == Side::Sell)
        && intent.passive != 0
        && intent.post_only != 0;
}

} // namespace

bool ExecutionAdmission::quote_buy_notional_microdollars(
    const StrategyIntent& intent,
    std::int32_t tick_size_e4,
    std::int64_t& out_microdollars) noexcept {

    out_microdollars = 0;
    if (intent.type != IntentType::Quote || intent.side != Side::Buy
        || !valid_quote_identity(intent) || tick_size_e4 <= 0) {
        return false;
    }

    // Prediction-market quote prices must be strictly inside (0, 1). Avoid a
    // generic multiply overflow by bounding the tick index before constructing
    // the e4 price. price_e4=5200 means $0.52/share.
    constexpr std::int64_t kPriceScale = 10'000;
    const auto tick = static_cast<std::int64_t>(tick_size_e4);
    if (tick >= kPriceScale) return false;
    if (intent.price_tick > (kPriceScale - 1) / tick) return false;
    const std::int64_t price_e4 = intent.price_tick * tick;
    if (price_e4 <= 0 || price_e4 >= kPriceScale) return false;

    // quantity is share microunits; dollars are also expressed as microdollars:
    //   q_microshares * price_e4 / 10,000 = collateral_microdollars.
    // Round upward so capital admission is conservative at sub-microdollar
    // boundaries rather than ever under-reserving.
    return mul_div_ceil_positive(
        intent.quantity_microunits, price_e4, kPriceScale, out_microdollars);
}

ExecutionAdmissionResult ExecutionAdmission::admit(
    const StrategyIntent& intent,
    std::int32_t tick_size_e4,
    SleeveCapitalAccount& capital) noexcept {

    ExecutionAdmissionResult result;

    // Never let capital pressure block a CANCEL/WITHDRAW/KILL. These still pass
    // OMS/transport validation downstream, but they do not wait for new-capital
    // accounting and therefore preserve the risk-off priority invariant.
    if (is_critical_intent(intent.type) || intent.urgency == Urgency::Critical) {
        result.reason = ExecutionAdmissionReason::ControlBypass;
        result.accepted = 1;
        return result;
    }

    if (intent.type != IntentType::Quote) {
        result.reason = ExecutionAdmissionReason::UnsupportedIntent;
        return result;
    }
    if (!valid_quote_identity(intent)) {
        result.reason = ExecutionAdmissionReason::InvalidIntent;
        return result;
    }
    if (tick_size_e4 <= 0) {
        result.reason = ExecutionAdmissionReason::InvalidTick;
        return result;
    }

    if (intent.side == Side::Sell) {
        // No new cash is reserved here. The execution policy must prove owned,
        // unreserved inventory before this SELL can become live.
        result.reason = ExecutionAdmissionReason::Accepted;
        result.accepted = 1;
        return result;
    }

    std::int64_t notional = 0;
    if (!quote_buy_notional_microdollars(intent, tick_size_e4, notional)) {
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
