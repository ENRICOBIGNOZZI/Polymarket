#pragma once
#include "pm/v7_clob_eip712.hpp"
#include <array>
#include <cstddef>
#include <cstdint>
#include <span>
#include <string_view>
namespace pm::v7::poly1271 {
using Hash32 = pm::v7::clob_eip712::Hash32;
constexpr std::size_t kWrappedSignatureHexChars = 636;
class PreparedHasher final {
public:
 PreparedHasher(std::uint64_t chain_id,std::string_view order_signer,
                const Hash32& app_domain_separator) noexcept;
 [[nodiscard]] bool valid() const noexcept { return valid_; }
 [[nodiscard]] bool digest(const Hash32& contents_hash,Hash32& output) noexcept;
private:
 std::array<std::uint8_t,7*32> encoded_{};
 std::array<std::uint8_t,66> envelope_{};
 bool valid_=false;
};
[[nodiscard]] bool wrap_signature_hex(
 std::span<const std::uint8_t,65> inner_signature,
 const Hash32& app_domain_separator,
 const Hash32& contents_hash,
 std::span<char> output) noexcept;
} // namespace pm::v7::poly1271
