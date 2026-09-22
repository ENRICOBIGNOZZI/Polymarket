#pragma once

#include "pm/v7_clob_wire.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <span>
#include <string_view>

namespace pm::v7::clob_wire {

// Static fields for one instrument/side execution lane. They are validated and
// serialized once during cold construction instead of on every order.
struct PreparedMarketOrderStaticView {
    std::string_view builder;
    std::string_view expiration;
    std::string_view maker;
    std::string_view metadata;
    std::string_view side;
    std::uint8_t signature_type = 0;
    std::string_view signer;
    std::string_view token_id;
    std::string_view owner;
    MarketOrderType order_type = MarketOrderType::FAK;
    bool post_only = false;
};

// Per-order fields that genuinely change on the hot path.
struct MarketOrderDynamicView {
    std::string_view maker_amount;
    std::string_view salt_decimal;
    std::string_view signature;
    std::string_view taker_amount;
    std::string_view timestamp_ms;
};

class PreparedMarketOrderJson final {
public:
    explicit PreparedMarketOrderJson(
        const PreparedMarketOrderStaticView& fixed) noexcept;

    [[nodiscard]] bool valid() const noexcept { return valid_; }

    // Length arithmetic only. Dynamic content validation remains centralized in
    // serialize(), so fused request builders can reserve the exact final body
    // offset without scanning the five dynamic strings twice.
    [[nodiscard]] std::size_t serialized_size(
        const MarketOrderDynamicView& dynamic) const noexcept;

    // Exact byte-compatible replacement for serialize_post_market_order() for
    // the frozen static lane. Caller-owned output; no allocation.
    [[nodiscard]] std::size_t serialize(
        const MarketOrderDynamicView& dynamic,
        std::span<char> output) const noexcept;

private:
    static constexpr std::size_t kChunkCount = 6;
    std::array<char, 2048> static_bytes_{};
    std::array<std::size_t, kChunkCount> offsets_{};
    std::array<std::size_t, kChunkCount> sizes_{};
    std::size_t static_size_ = 0;
    bool valid_ = false;
};

} // namespace pm::v7::clob_wire
