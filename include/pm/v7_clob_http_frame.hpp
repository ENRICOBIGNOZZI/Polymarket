#pragma once

#include <array>
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

// Hot-path form for one authenticated execution lane. Address, API key and
// passphrase are validated and serialized once during construction. Exposing
// the exact dynamic header size lets a fused request builder place the JSON body
// directly at its final offset before HMAC signing it in place.
class PreparedPostOrderHttp1 final {
public:
    PreparedPostOrderHttp1(std::string_view address,
                           std::string_view api_key,
                           std::string_view passphrase) noexcept;

    [[nodiscard]] bool valid() const noexcept { return valid_; }

    [[nodiscard]] std::size_t required_header_size(
        std::size_t signature_size,
        std::size_t timestamp_size,
        std::size_t body_size) const noexcept;

    [[nodiscard]] std::size_t serialize_headers(
        std::string_view signature,
        std::string_view timestamp,
        std::size_t body_size,
        std::span<char> output) const noexcept;

    [[nodiscard]] std::size_t serialize(
        std::string_view signature,
        std::string_view timestamp,
        std::string_view exact_body,
        std::span<char> output) const noexcept;

private:
    std::array<char, 512> prefix_{};
    std::array<char, 512> after_timestamp_{};
    std::size_t prefix_size_ = 0;
    std::size_t after_timestamp_size_ = 0;
    bool valid_ = false;
};

} // namespace pm::v7::clob_http_frame
