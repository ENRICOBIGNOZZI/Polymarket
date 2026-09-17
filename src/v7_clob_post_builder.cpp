#include "pm/v7_clob_post_builder.hpp"

#include <array>
#include <limits>

namespace pm::v7::clob_post {
namespace {
using clob_wire::PostMarketOrderView;

bool add_size(std::size_t& total, std::size_t value) noexcept {
    if (value > std::numeric_limits<std::size_t>::max() - total) return false;
    total += value;
    return true;
}

bool add_quoted_size(std::size_t& total, std::string_view value) noexcept {
    return add_size(total, value.size()) && add_size(total, 2);
}

// Length arithmetic only. Content validation remains centralized in
// serialize_post_market_order(), exactly once per build.
std::size_t body_size_unchecked(const PostMarketOrderView& request) noexcept {
    constexpr std::array<std::string_view, 15> separators{
        "{\"deferExec\":false,\"order\":{\"builder\":",
        ",\"expiration\":",
        ",\"maker\":",
        ",\"makerAmount\":",
        ",\"metadata\":",
        ",\"salt\":",
        ",\"side\":",
        ",\"signature\":",
        ",\"signatureType\":",
        ",\"signer\":",
        ",\"takerAmount\":",
        ",\"timestamp\":",
        ",\"tokenId\":",
        "},\"orderType\":\"",
        "\",\"owner\":",
    };

    std::size_t total = 0;
    for (const auto part : separators) if (!add_size(total, part.size())) return 0;
    const auto& order = request.order;
    if (!add_quoted_size(total, order.builder)
        || !add_quoted_size(total, order.expiration)
        || !add_quoted_size(total, order.maker)
        || !add_quoted_size(total, order.maker_amount)
        || !add_quoted_size(total, order.metadata)
        || !add_size(total, order.salt_decimal.size())
        || !add_quoted_size(total, order.side)
        || !add_quoted_size(total, order.signature)
        || !add_size(total, 1) // signatureType is validated to one digit: 0..3.
        || !add_quoted_size(total, order.signer)
        || !add_quoted_size(total, order.taker_amount)
        || !add_quoted_size(total, order.timestamp_ms)
        || !add_quoted_size(total, order.token_id)
        || !add_size(total, 3) // FAK/FOK.
        || !add_quoted_size(total, request.owner)
        || !add_size(total, 1)) return 0; // final '}'
    return total;
}
}

PreparedPostOrderBuilder::PreparedPostOrderBuilder(
    std::string_view address,
    std::string_view api_key,
    std::string_view passphrase,
    std::string_view base64_l2_secret) noexcept
    : http_(address, api_key, passphrase), hmac_(base64_l2_secret) {}

bool PreparedPostOrderBuilder::valid() const noexcept {
    return http_.valid() && hmac_.valid();
}

std::size_t PreparedPostOrderBuilder::build(
    const clob_wire::PostMarketOrderView& request,
    std::string_view request_timestamp,
    std::span<char> output) noexcept {
    if (!valid() || request_timestamp.empty()) return 0;

    const std::size_t body_size = body_size_unchecked(request);
    if (body_size == 0) return 0;
    const std::size_t header_size = http_.required_header_size(
        clob_wire::L2HmacSigner::kEncodedSignatureSize,
        request_timestamp.size(), body_size);
    if (header_size == 0 || header_size > output.size()
        || body_size > output.size() - header_size) return 0;

    auto body = output.subspan(header_size, body_size);
    const std::size_t serialized = clob_wire::serialize_post_market_order(request, body);
    if (serialized != body_size) return 0;

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
