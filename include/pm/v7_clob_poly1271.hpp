#pragma once

#include "pm/v7_clob_eip712.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <span>
#include <string_view>

namespace pm::v7::clob_poly1271 {

inline constexpr std::string_view kOrderContentsType =
    "Order(uint256 salt,address maker,address signer,uint256 tokenId,"
    "uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,"
    "uint256 timestamp,bytes32 metadata,bytes32 builder)";

// Build the official POLY_1271 wire wrapper around an already-created
// recoverable 65-byte inner ECDSA signature. No allocation.
[[nodiscard]] std::size_t wrap_signature(
    std::string_view inner_signature_hex,
    const pm::v7::clob_eip712::Hash32& app_domain_separator,
    const pm::v7::clob_eip712::Hash32& contents_hash,
    std::span<char> output) noexcept;

// Prepared DepositWallet/Solady digest stage. The wallet address and app-domain
// separator are frozen at construction; each order supplies only its contents
// hash. This is the digest signed inside a signatureType=3 wrapper.
class PreparedInnerDigest final {
public:
    PreparedInnerDigest(
        const pm::v7::clob_eip712::ExchangeV2DomainView& app_domain,
        std::string_view deposit_wallet) noexcept;

    [[nodiscard]] bool valid() const noexcept { return valid_; }
    [[nodiscard]] const pm::v7::clob_eip712::Hash32& app_domain_separator() const noexcept {
        return app_domain_separator_;
    }
    [[nodiscard]] bool digest(
        const pm::v7::clob_eip712::Hash32& contents_hash,
        pm::v7::clob_eip712::Hash32& output) noexcept;

private:
    pm::v7::clob_eip712::Hash32 app_domain_separator_{};
    std::array<std::uint8_t, 7 * 32> encoded_{};
    std::array<std::uint8_t, 66> envelope_{};
    bool valid_ = false;
};

} // namespace pm::v7::clob_poly1271
