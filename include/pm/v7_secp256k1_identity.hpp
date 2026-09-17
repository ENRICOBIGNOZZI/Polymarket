#pragma once

#include <array>
#include <cstdint>
#include <span>
#include <string_view>

namespace pm::v7::clob_signing {

using EthereumAddress20 = std::array<std::uint8_t, 20>;

// Cold-path identity primitive. Derives the Ethereum address owned by one
// secp256k1 secret key. No key is retained after the call returns.
[[nodiscard]] bool derive_ethereum_address(
    std::span<const std::uint8_t, 32> secret_key,
    EthereumAddress20& output) noexcept;

// Accepts a 40-hex Ethereum address with optional 0x prefix. Matching is on
// raw address bytes, so checksum capitalization is not authority-bearing.
[[nodiscard]] bool secret_key_matches_ethereum_address(
    std::span<const std::uint8_t, 32> secret_key,
    std::string_view expected_address) noexcept;

} // namespace pm::v7::clob_signing
