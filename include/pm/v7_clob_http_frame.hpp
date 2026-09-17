#pragma once

#include <cstddef>
#include <span>
#include <string_view>

namespace pm::v7::clob_http_frame {

struct L2AuthHeadersView {
    std::string_view address;
    std::string_view signature;
    std::string_view timestamp;
    std::string_view api_key;
    std::string_view passphrase;
};

// Builds one deterministic HTTP/1.1 POST /order frame into caller-owned
// memory. The body must be the exact body previously used for the CLOB L2 HMAC.
// Returns zero on invalid headers/body or insufficient capacity.
[[nodiscard]] std::size_t serialize_post_order_http1(
    const L2AuthHeadersView& auth,
    std::string_view exact_body,
    std::span<char> output) noexcept;

} // namespace pm::v7::clob_http_frame
