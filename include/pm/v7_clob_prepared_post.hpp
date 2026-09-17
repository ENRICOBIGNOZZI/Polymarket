#pragma once

#include "pm/v7_clob_http_frame.hpp"
#include "pm/v7_clob_prepared_order.hpp"
#include "pm/v7_clob_wire.hpp"

#include <cstddef>
#include <span>
#include <string_view>

namespace pm::v7::clob_post {

// One frozen instrument/side execution lane. Static order JSON, static HTTP
// headers and the HMAC key schedule are all prepared off the hot path.
class PreparedPostOrderBuilder final {
public:
    PreparedPostOrderBuilder(
        const clob_wire::PreparedMarketOrderStaticView& fixed_order,
        std::string_view address,
        std::string_view api_key,
        std::string_view passphrase,
        std::string_view base64_l2_secret) noexcept;

    [[nodiscard]] bool valid() const noexcept;

    // Serializes the exact order body directly into its final position in the
    // HTTP frame, HMACs that in-place span, then backfills the dynamic headers.
    // Returns zero on any validation, capacity or crypto failure.
    [[nodiscard]] std::size_t build(
        const clob_wire::MarketOrderDynamicView& dynamic,
        std::string_view request_timestamp,
        std::span<char> output) noexcept;

private:
    clob_wire::PreparedMarketOrderJson order_;
    clob_http_frame::PreparedPostOrderHttp1 http_;
    clob_wire::L2HmacSigner hmac_;
};

} // namespace pm::v7::clob_post
