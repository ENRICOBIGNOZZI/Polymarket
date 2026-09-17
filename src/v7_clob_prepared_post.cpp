#include "pm/v7_clob_prepared_post.hpp"

#include <array>

namespace pm::v7::clob_post {

PreparedPostOrderBuilder::PreparedPostOrderBuilder(
    const clob_wire::PreparedMarketOrderStaticView& fixed_order,
    std::string_view address,
    std::string_view api_key,
    std::string_view passphrase,
    std::string_view base64_l2_secret) noexcept
    : order_(fixed_order),
      http_(address, api_key, passphrase),
      hmac_(base64_l2_secret) {}

bool PreparedPostOrderBuilder::valid() const noexcept {
    return order_.valid() && http_.valid() && hmac_.valid();
}

std::size_t PreparedPostOrderBuilder::build(
    const clob_wire::MarketOrderDynamicView& dynamic,
    std::string_view request_timestamp,
    std::span<char> output) noexcept {
    if (!valid() || request_timestamp.empty()) return 0;

    // Length arithmetic only: dynamic content is scanned exactly once below by
    // order_.serialize(). This gives the final body offset without a temp body.
    const std::size_t body_size = order_.serialized_size(dynamic);
    if (body_size == 0) return 0;

    const std::size_t header_size = http_.required_header_size(
        clob_wire::L2HmacSigner::kEncodedSignatureSize,
        request_timestamp.size(), body_size);
    if (header_size == 0 || header_size > output.size()
        || body_size > output.size() - header_size) return 0;

    auto body = output.subspan(header_size, body_size);
    if (order_.serialize(dynamic, body) != body_size) return 0;

    std::array<char, clob_wire::L2HmacSigner::kEncodedSignatureSize> signature{};
    const std::size_t signature_size = hmac_.sign(request_timestamp, body, signature);
    if (signature_size != signature.size()) return 0;

    const std::size_t written_header = http_.serialize_headers(
        std::string_view(signature.data(), signature_size),
        request_timestamp, body_size, output.first(header_size));
    if (written_header != header_size) return 0;

    return header_size + body_size;
}

} // namespace pm::v7::clob_post
