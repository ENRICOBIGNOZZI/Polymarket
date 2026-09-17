#include "pm/v7_clob_order_amounts.hpp"

#include <limits>

namespace pm::v7::clob_order {
namespace {
constexpr std::int64_t kPriceScale = 10'000;
constexpr std::int64_t kSizeTwoDecimalQuantum = 10'000;
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
        || !supported_exchange_v2_tick_e4(tick_size_e4)
        || price_e4 <= 0 || price_e4 >= kPriceScale
        || price_e4 % tick_size_e4 != 0
        || quantity_microunits <= 0) {
        return out;
    }

    // Official V2 limit-order builder: floor(size * 100) / 100.
    const std::int64_t rounded_quantity =
        (quantity_microunits / kSizeTwoDecimalQuantum) * kSizeTwoDecimalQuantum;
    if (rounded_quantity <= 0) return out;

    if (rounded_quantity > std::numeric_limits<std::int64_t>::max() / price_e4) {
        return out;
    }
    const std::int64_t product = rounded_quantity * static_cast<std::int64_t>(price_e4);
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
