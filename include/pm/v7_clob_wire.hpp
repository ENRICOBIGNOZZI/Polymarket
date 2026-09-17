#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <span>
#include <string_view>

struct evp_mac_st;
struct evp_mac_ctx_st;

namespace pm::v7::clob_wire {

// Narrow hot-path contract for already EIP-712-signed CLOB market orders.
// The wallet/order signature is intentionally outside this component.
enum class MarketOrderType : std::uint8_t { FAK = 1, FOK = 2 };

struct SignedMarketOrderView {
    std::string_view builder;
    std::string_view expiration;
    std::string_view maker;
    std::string_view maker_amount;
    std::string_view metadata;
    std::string_view salt_decimal;
    std::string_view side;
    std::string_view signature;
    std::uint8_t signature_type = 0;
    std::string_view signer;
    std::string_view taker_amount;
    std::string_view timestamp_ms;
    std::string_view token_id;
};

struct PostMarketOrderView {
    SignedMarketOrderView order{};
    std::string_view owner;
    MarketOrderType order_type = MarketOrderType::FAK;
};

// Serializes the exact body that must also be passed to L2HmacSigner::sign().
// Returns zero on validation failure or insufficient output capacity.
// No heap allocation is performed.
[[nodiscard]] std::size_t serialize_post_market_order(
    const PostMarketOrderView& request, std::span<char> output) noexcept;

// Single-owner, reusable CLOB L2 signer. Construction is cold-path; sign() is
// reuses a cold-path OpenSSL MAC context and hashes timestamp + "POST" +
// "/order" + exact_body without application-side message concatenation.
class L2HmacSigner final {
public:
    explicit L2HmacSigner(std::string_view base64_secret) noexcept;
    ~L2HmacSigner();

    L2HmacSigner(const L2HmacSigner&) = delete;
    L2HmacSigner& operator=(const L2HmacSigner&) = delete;
    L2HmacSigner(L2HmacSigner&&) = delete;
    L2HmacSigner& operator=(L2HmacSigner&&) = delete;

    [[nodiscard]] bool valid() const noexcept { return valid_; }

    // URL-safe base64 with padding, matching POLY_SIGNATURE. Returns bytes
    // written (normally 44 for HMAC-SHA256) or zero on failure.
    [[nodiscard]] std::size_t sign(
        std::string_view request_timestamp,
        std::string_view exact_body,
        std::span<char> output) noexcept;

private:
    std::array<unsigned char, 128> key_{};
    std::size_t key_size_ = 0;
    evp_mac_st* mac_ = nullptr;
    evp_mac_ctx_st* ctx_ = nullptr;
    bool valid_ = false;
};

} // namespace pm::v7::clob_wire
