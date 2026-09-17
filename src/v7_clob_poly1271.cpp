#include "pm/v7_clob_poly1271.hpp"

#include <array>
#include <cstdint>
#include <cstring>

namespace pm::v7::clob_poly1271 {
namespace {

using pm::v7::clob_eip712::Hash32;

constexpr Hash32 kSoladyTypeHash{0x6bU, 0xa0U, 0x28U, 0x56U, 0x5cU, 0xb3U, 0x24U, 0xc2U, 0xaaU, 0x02U, 0xbbU, 0x71U, 0x4bU, 0x98U, 0x16U, 0xd0U, 0xbdU, 0xddU, 0x55U, 0x7aU, 0x2fU, 0x33U, 0xbbU, 0x36U, 0xcfU, 0x13U, 0x27U, 0x2aU, 0x42U, 0x56U, 0xbdU, 0x42U};
constexpr Hash32 kDepositWalletNameHash{0xd6U, 0x82U, 0xb5U, 0x29U, 0xa1U, 0x7cU, 0xdaU, 0x19U, 0xaaU, 0x27U, 0x5fU, 0x3aU, 0x05U, 0x06U, 0x08U, 0xf9U, 0xe9U, 0x40U, 0x1fU, 0xadU, 0xd1U, 0xb0U, 0xd2U, 0x33U, 0xd8U, 0x15U, 0x19U, 0x97U, 0x22U, 0x95U, 0x82U, 0x8bU};
constexpr Hash32 kDepositWalletVersionHash{0xc8U, 0x9eU, 0xfdU, 0xaaU, 0x54U, 0xc0U, 0xf2U, 0x0cU, 0x7aU, 0xdfU, 0x61U, 0x28U, 0x82U, 0xdfU, 0x09U, 0x50U, 0xf5U, 0xa9U, 0x51U, 0x63U, 0x7eU, 0x03U, 0x07U, 0xcdU, 0xcbU, 0x4cU, 0x67U, 0x2fU, 0x29U, 0x8bU, 0x8bU, 0xc6U};

[[nodiscard]] bool hex_char(char c) noexcept {
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
}

int hex_value(char c) noexcept {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}
bool parse_address(std::string_view address, std::array<std::uint8_t, 32>& word) noexcept {
    if (address.size() != 42 || address[0] != '0'
        || (address[1] != 'x' && address[1] != 'X')) return false;
    word.fill(0);
    for (std::size_t i = 0; i < 20; ++i) {
        const int hi = hex_value(address[2 + i * 2]);
        const int lo = hex_value(address[3 + i * 2]);
        if (hi < 0 || lo < 0) return false;
        word[12 + i] = static_cast<std::uint8_t>((hi << 4) | lo);
    }
    return true;
}

void put_word(std::span<std::uint8_t> encoded, std::size_t index,
              const Hash32& word) noexcept {
    std::memcpy(encoded.data() + index * 32, word.data(), word.size());
}

void encode_uint64(std::uint64_t value, Hash32& word) noexcept {
    word.fill(0);
    for (std::size_t i = 0; i < 8; ++i) {
        word[31 - i] = static_cast<std::uint8_t>(value & 0xffU);
        value >>= 8U;
    }
}

char lower_hex(char c) noexcept {
    return c >= 'A' && c <= 'F' ? static_cast<char>(c - 'A' + 'a') : c;
}
void append_hash(const Hash32& hash,
                 std::span<char> output, std::size_t& position) noexcept {
    static constexpr char kHex[] = "0123456789abcdef";
    for (const auto byte : hash) {
        output[position++] = kHex[byte >> 4U];
        output[position++] = kHex[byte & 0x0fU];
    }
}

} // namespace

PreparedInnerDigest::PreparedInnerDigest(
    const pm::v7::clob_eip712::ExchangeV2DomainView& app_domain,
    std::string_view deposit_wallet) noexcept {
    if (!pm::v7::clob_eip712::exchange_v2_domain_separator(app_domain, app_domain_separator_)) return;
    Hash32 word{};
    put_word(encoded_, 0, kSoladyTypeHash);
    put_word(encoded_, 2, kDepositWalletNameHash);
    put_word(encoded_, 3, kDepositWalletVersionHash);
    encode_uint64(app_domain.chain_id, word);
    put_word(encoded_, 4, word);
    if (!parse_address(deposit_wallet, word)) return;
    put_word(encoded_, 5, word);
    envelope_[0] = 0x19U;
    envelope_[1] = 0x01U;
    std::memcpy(envelope_.data() + 2, app_domain_separator_.data(), app_domain_separator_.size());
    valid_ = true;
}
bool PreparedInnerDigest::digest(const Hash32& contents_hash, Hash32& output) noexcept {
    if (!valid_) return false;
    put_word(encoded_, 1, contents_hash);
    const Hash32 typed_data_sign_struct_hash = pm::v7::clob_eip712::keccak256(encoded_);
    std::memcpy(envelope_.data() + 34, typed_data_sign_struct_hash.data(), typed_data_sign_struct_hash.size());
    output = pm::v7::clob_eip712::keccak256(envelope_);
    return true;
}

std::size_t wrap_signature(
    std::string_view inner_signature_hex,
    const Hash32& app_domain_separator,
    const Hash32& contents_hash,
    std::span<char> output) noexcept {
    if (inner_signature_hex.starts_with("0x") || inner_signature_hex.starts_with("0X")) {
        inner_signature_hex.remove_prefix(2);
    }
    if (inner_signature_hex.size() != 130) return 0;
    for (const char c : inner_signature_hex) if (!hex_char(c)) return 0;

    constexpr std::size_t type_hex_size = kOrderContentsType.size() * 2U;
    constexpr std::size_t required = 2U + 130U + 64U + 64U + type_hex_size + 4U;
    if (output.size() < required || kOrderContentsType.size() > 0xffffU) return 0;

    std::size_t position = 0;
    output[position++] = '0'; output[position++] = 'x';
    for (const char c : inner_signature_hex) output[position++] = lower_hex(c);
    append_hash(app_domain_separator, output, position);
    append_hash(contents_hash, output, position);
    static constexpr char kHex[] = "0123456789abcdef";
    for (const unsigned char c : kOrderContentsType) {
        output[position++] = kHex[c >> 4U];
        output[position++] = kHex[c & 0x0fU];
    }
    const auto length = static_cast<std::uint16_t>(kOrderContentsType.size());
    output[position++] = kHex[(length >> 12U) & 0x0fU];
    output[position++] = kHex[(length >> 8U) & 0x0fU];
    output[position++] = kHex[(length >> 4U) & 0x0fU];
    output[position++] = kHex[length & 0x0fU];
    return position == required ? position : 0;
}

} // namespace pm::v7::clob_poly1271
