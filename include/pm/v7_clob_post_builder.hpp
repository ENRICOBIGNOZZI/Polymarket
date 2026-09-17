#pragma once

#include "pm/v7_clob_http_frame.hpp"
#include "pm/v7_clob_wire.hpp"

#include <cstddef>
#include <span>
#include <string_view>

namespace pm::v7::clob_post {

// Single-owner native order-frame builder. Static L2 HTTP credentials and HMAC
// key state are prepared once. On each order the JSON body is serialized
// directly into its final HTTP-frame position, signed there, then headers are
// backfilled before it. No intermediate body buffer or body memcpy is needed.
class PreparedPostOrderBuilder final {
public:
    PreparedPostOrderBuilder(std::string_view address,
                             std::string_view api_key,
                             std::string_view passphrase,
                             std::string_view base64_l2_secret) noexcept;

    [[nodiscard]] bool valid() const noexcept;

    // Returns the complete HTTP/1.1 frame size, or zero on any validation,
    // capacity or crypto failure. A zero result must never be submitted.
    [[nodiscard]] std::size_t build(
        const clob_wire::PostMarketOrderView& request,
        std::string_view request_timestamp,
        std::span<char> output) noexcept;

private:
    clob_http_frame::PreparedPostOrderHttp1 http_;
    clob_wire::L2HmacSigner hmac_;
};

} // namespace pm::v7::clob_post
