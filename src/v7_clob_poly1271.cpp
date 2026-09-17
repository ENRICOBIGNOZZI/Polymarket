#include "pm/v7_clob_poly1271.hpp"

#include <array>
#include <cstdint>

namespace pm::v7::clob_poly1271 {
namespace {

[[nodiscard]] bool hex_char(char c) noexcept {
    return (c >= '0' && c <= '9')
        || (c >= 'a' && c <= 'f')
        || (c >= 'A' && c <= 'F');
}

char lower_hex(char c) noexcept {
    return c >= 'A' && c <= 'F' ? static_cast<char>(c - 'A' + 'a') : c;
}

void append_hash(const pm::v7::clob_eip712::Hash32& hash,
                 std::span<char> output, std::size_t& position) noexcept {
    static constexpr char kHex[] = "0123456789abcdef";
    for (const auto byte : hash) {
        output[position++] = kHex[byte >> 4U];
        output[position++] = kHex[byte & 0x0fU];
    }
}

} // namespace
std::size_t wrap_signature(
    std::string_view inner_signature_hex,
    const pm::v7::clob_eip712::Hash32& app_domain_separator,
    const pm::v7::clob_eip712::Hash32& contents_hash,
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
