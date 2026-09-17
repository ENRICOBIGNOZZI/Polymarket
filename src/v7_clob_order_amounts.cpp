#include "pm/v7_clob_order_amounts.hpp"

#include <limits>

namespace pm::v7::clob_order {
namespace {
constexpr std::int64_t kPriceScale = 10'000;
constexpr std::int64_t kSizeTwoDecimalQuantum = 10'000;

[[nodiscard]] bool price_aligned_to_supported_tick(
    std::int32_t price_e4, std::int32_t tick_size_e4) noexcept {
    // Keep the divisor compile-time constant in every supported arm. On the
    // order path this avoids a variable integer divide solely to re-check tick
    // alignment that has only six legal values.
    switch (tick_size_e4) {
        case 1000: return price_e4 % 1000 == 0;
        case 100: return price_e4 % 100 == 0;
        case 50: return price_e4 % 50 == 0;
        case 25: return price_e4 % 25 == 0;
        case 10: return price_e4 % 10 == 0;
        case 1: return true;
        default: return false;
    }
}

[[nodiscard]] bool multiply_price_quantity(
    std::int64_t quantity, std::int32_t price_e4,
    std::int64_t& product) noexcept {
#if defined(__clang__) || defined(__GNUC__)
    return !__builtin_mul_overflow(
        quantity, static_cast<std::int64_t>(price_e4), &product);
#else
    if (quantity > std::numeric_limits<std::int64_t>::max() / price_e4) return false;
    product = quantity * static_cast<std::int64_t>(price_e4);
    return true;
#endif
}
}

bool supported_exchange_v2_tick_e4(std::int32_t tick) noexcept {
    switch (tick) {
        case 1000: // 0.1
        case 100:  // 0.01
        case 50:   // 0.005
        case 25:   // 0.0025
        case 10:   // 0.001
        case 1:    // 0.0001
            return true;
        default:
            return false;
    }
}

ExchangeV2Amounts marketable_limit_amounts(
    Side side,
    std::int32_t price_e4,
    std::int32_t tick_size_e4,
    std::int64_t quantity_microunits) noexcept {
    ExchangeV2Amounts out;
    if ((side != Side::Buy && side != Side::Sell)
        || price_e4 <= 0 || price_e4 >= kPriceScale
        || !price_aligned_to_supported_tick(price_e4, tick_size_e4)
        || quantity_microunits <= 0) {
        return out;
    }

    // Official V2 limit-order builder: floor(size * 100) / 100.
    const std::int64_t rounded_quantity =
        (quantity_microunits / kSizeTwoDecimalQuantum) * kSizeTwoDecimalQuantum;
    if (rounded_quantity <= 0) return out;

    std::int64_t product = 0;
    if (!multiply_price_quantity(rounded_quantity, price_e4, product)) return out;
    if (product % kPriceScale != 0) return out;
    const std::int64_t notional_microunits = product / kPriceScale;
    if (notional_microunits <= 0) return out;

    out.rounded_quantity_microunits = rounded_quantity;
    if (side == Side::Buy) {
        out.maker_amount = notional_microunits;
        out.taker_amount = rounded_quantity;
    } else {
        out.maker_amount = rounded_quantity;
        out.taker_amount = notional_microunits;
    }
    out.valid = 1;
    return out;
}

} // namespace pm::v7::clob_order
