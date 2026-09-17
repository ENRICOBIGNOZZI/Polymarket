#pragma once

#include <cstdint>
#include <string_view>

namespace pm::v7::clob_signing {

enum class SignatureMode : std::uint8_t {
    Eoa = 0,
    PolyProxy = 1,
    PolyGnosisSafe = 2,
    Poly1271 = 3,
};

struct SigningContract {
    SignatureMode mode = SignatureMode::Eoa;
    std::string_view maker;
    std::string_view order_signer;
    std::string_view key_signer;
    std::uint8_t requires_poly1271_wrap = 0;
    std::uint8_t valid = 0;
};

[[nodiscard]] bool parse_signature_mode(
    std::string_view text, SignatureMode& output) noexcept;
[[nodiscard]] bool valid_eth_address(std::string_view address) noexcept;
[[nodiscard]] SigningContract build_signing_contract(
    SignatureMode mode,
    std::string_view maker,
    std::string_view key_signer) noexcept;

} // namespace pm::v7::clob_signing
