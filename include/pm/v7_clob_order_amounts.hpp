#pragma once

#include "pm/v7_intent.hpp"

#include <cstdint>
#include <type_traits>

namespace pm::v7::clob_order {

struct ExchangeV2Amounts {
    std::int64_t maker_amount = 0;
    std::int64_t taker_amount = 0;
    std::int64_t rounded_quantity_microunits = 0;
    std::uint8_t valid = 0;
};

[[nodiscard]] ExchangeV2Amounts marketable_limit_amounts(
    Side side,
    std::int32_t price_e4,
    std::int32_t tick_size_e4,
    std::int64_t quantity_microunits) noexcept;

[[nodiscard]] bool supported_exchange_v2_tick_e4(std::int32_t tick_size_e4) noexcept;

static_assert(std::is_trivially_copyable_v<ExchangeV2Amounts>);

} // namespace pm::v7::clob_order
