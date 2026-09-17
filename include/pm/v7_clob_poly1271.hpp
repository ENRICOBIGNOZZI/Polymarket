#pragma once

#include "pm/v7_clob_eip712.hpp"

#include <cstddef>
#include <span>
#include <string_view>

namespace pm::v7::clob_poly1271 {

// Current official Exchange V2 contents type used by DepositWallet/Solady
// POLY_1271 signatures.
inline constexpr std::string_view kOrderContentsType =
    "Order(uint256 salt,address maker,address signer,uint256 tokenId,"
    "uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,"
    "uint256 timestamp,bytes32 metadata,bytes32 builder)";

// Builds the official POLY_1271 wire signature from an already-created
// recoverable 65-byte inner ECDSA signature plus the app-domain separator and
// Order contents hash. Returns bytes written as lowercase 0x-prefixed hex, or
// zero on malformed input / insufficient output capacity. No allocation.
[[nodiscard]] std::size_t wrap_signature(
    std::string_view inner_signature_hex,
    const pm::v7::clob_eip712::Hash32& app_domain_separator,
    const pm::v7::clob_eip712::Hash32& contents_hash,
    std::span<char> output) noexcept;

} // namespace pm::v7::clob_poly1271
