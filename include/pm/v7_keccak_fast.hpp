#pragma once

#include "pm/v7_clob_eip712.hpp"

#include <cstdint>
#include <span>

namespace pm::v7::clob_eip712 {

// Portable Ethereum Keccak-256 candidate with the same semantics as keccak256().
// The F1600 permutation is explicitly unrolled to remove data-dependent loop /
// modulo/index overhead from the EIP-712 order-signing path.
[[nodiscard]] Hash32 keccak256_unrolled(std::span<const std::uint8_t> input) noexcept;

} // namespace pm::v7::clob_eip712
