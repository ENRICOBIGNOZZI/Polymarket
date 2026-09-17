#include "pm/v7_secp256k1_identity.hpp"
#include "pm/v7_clob_eip712.hpp"

#include <secp256k1.h>

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>

namespace pm::v7::clob_signing {
namespace {

[[nodiscard]] int hex_value(char c) noexcept {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

[[nodiscard]] bool parse_address(
    std::string_view text, EthereumAddress20& output) noexcept {
    if (text.starts_with("0x") || text.starts_with("0X")) text.remove_prefix(2);
    if (text.size() != 40) return false;
    for (std::size_t i = 0; i < output.size(); ++i) {
        const int hi = hex_value(text[2 * i]);
        const int lo = hex_value(text[2 * i + 1]);
        if (hi < 0 || lo < 0) return false;
        output[i] = static_cast<std::uint8_t>((hi << 4) | lo);
    }
    return true;
}

} // namespace

bool derive_ethereum_address(
    std::span<const std::uint8_t, 32> secret_key,
    EthereumAddress20& output) noexcept {
    secp256k1_context* context =
        secp256k1_context_create(SECP256K1_CONTEXT_VERIFY);
    if (context == nullptr) return false;

    bool ok = false;
    secp256k1_pubkey public_key{};
    if (secp256k1_ec_seckey_verify(context, secret_key.data()) == 1
        && secp256k1_ec_pubkey_create(context, &public_key, secret_key.data()) == 1) {
        std::array<unsigned char, 65> serialized{};
        std::size_t serialized_size = serialized.size();
        if (secp256k1_ec_pubkey_serialize(
                context, serialized.data(), &serialized_size, &public_key,
                SECP256K1_EC_UNCOMPRESSED) == 1
            && serialized_size == serialized.size() && serialized[0] == 0x04U) {
            const auto digest = pm::v7::clob_eip712::keccak256(
                std::span<const std::uint8_t>(serialized.data() + 1, 64));
            std::copy(digest.end() - output.size(), digest.end(), output.begin());
            ok = true;
        }
    }
    secp256k1_context_destroy(context);
    return ok;
}

bool secret_key_matches_ethereum_address(
    std::span<const std::uint8_t, 32> secret_key,
    std::string_view expected_address) noexcept {
    EthereumAddress20 expected{};
    EthereumAddress20 derived{};
    if (!parse_address(expected_address, expected)) return false;
    if (!derive_ethereum_address(secret_key, derived)) return false;
    return derived == expected;
}

} // namespace pm::v7::clob_signing
