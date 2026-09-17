#pragma once

#include "pm/v7_clob_wire.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <span>
#include <string_view>

namespace pm::v7::clob_prepared_body {

struct StaticOrderView {
    std::string_view builder;
    std::string_view expiration;
    std::string_view maker;
    std::string_view metadata;
    std::string_view side;
    std::uint8_t signature_type = 0;
    std::string_view signer;
    std::string_view token_id;
    std::string_view owner;
    pm::v7::clob_wire::MarketOrderType order_type =
        pm::v7::clob_wire::MarketOrderType::FAK;
};

class PreparedPostMarketOrderBody final {
public:
    explicit PreparedPostMarketOrderBody(const StaticOrderView& fixed) noexcept;
    [[nodiscard]] bool valid() const noexcept { return valid_; }

    [[nodiscard]] std::size_t serialize(
        std::string_view maker_amount,
        std::string_view salt_decimal,
        std::string_view signature,
        std::string_view taker_amount,
        std::string_view timestamp_ms,
        std::span<char> output) const noexcept;

private:
    static constexpr std::size_t kSegments = 6;
    std::array<char, 2048> storage_{};
    std::array<std::uint16_t, kSegments> offsets_{};
    std::array<std::uint16_t, kSegments> sizes_{};
    bool valid_ = false;
};

} // namespace pm::v7::clob_prepared_body
