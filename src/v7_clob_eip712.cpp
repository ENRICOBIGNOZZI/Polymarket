#include "pm/v7_clob_eip712.hpp"

#include <algorithm>
#include <array>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <cstring>

namespace pm::v7::clob_eip712 {
namespace {

constexpr std::size_t kKeccakRate = 136;

constexpr std::array<std::uint64_t, 24> kRoundConstants{
    0x0000000000000001ULL, 0x0000000000008082ULL,
    0x800000000000808aULL, 0x8000000080008000ULL,
    0x000000000000808bULL, 0x0000000080000001ULL,
    0x8000000080008081ULL, 0x8000000000008009ULL,
    0x000000000000008aULL, 0x0000000000000088ULL,
    0x0000000080008009ULL, 0x000000008000000aULL,
    0x000000008000808bULL, 0x800000000000008bULL,
    0x8000000000008089ULL, 0x8000000000008003ULL,
    0x8000000000008002ULL, 0x8000000000000080ULL,
    0x000000000000800aULL, 0x800000008000000aULL,
    0x8000000080008081ULL, 0x8000000000008080ULL,
    0x0000000080000001ULL, 0x8000000080008008ULL,
};
constexpr std::array<unsigned, 24> kRotation{
    1, 3, 6, 10, 15, 21, 28, 36, 45, 55, 2, 14,
    27, 41, 56, 8, 25, 43, 62, 18, 39, 61, 20, 44,
};
constexpr std::array<unsigned, 24> kPiLane{
    10, 7, 11, 17, 18, 3, 5, 16, 8, 21, 24, 4,
    15, 23, 19, 13, 12, 2, 20, 14, 22, 9, 6, 1,
};

std::uint64_t load64_le(const std::uint8_t* p) noexcept {
    std::uint64_t value = 0;
    for (unsigned i = 0; i < 8; ++i) value |= static_cast<std::uint64_t>(p[i]) << (8U * i);
    return value;
}

void store64_le(std::uint64_t value, std::uint8_t* p) noexcept {
    for (unsigned i = 0; i < 8; ++i) p[i] = static_cast<std::uint8_t>(value >> (8U * i));
}

void keccak_f1600(std::array<std::uint64_t, 25>& state) noexcept {
    std::array<std::uint64_t, 5> column{};
    for (std::size_t round = 0; round < kRoundConstants.size(); ++round) {
        for (std::size_t i = 0; i < 5; ++i) {
            column[i] = state[i] ^ state[i + 5] ^ state[i + 10] ^ state[i + 15] ^ state[i + 20];
        }
        for (std::size_t i = 0; i < 5; ++i) {
            const std::uint64_t t = column[(i + 4) % 5] ^ std::rotl(column[(i + 1) % 5], 1);
            for (std::size_t j = 0; j < 25; j += 5) state[j + i] ^= t;
        }

        std::uint64_t t = state[1];
        for (std::size_t i = 0; i < 24; ++i) {
            const std::size_t lane = kPiLane[i];
            const std::uint64_t saved = state[lane];
            state[lane] = std::rotl(t, static_cast<int>(kRotation[i]));
            t = saved;
        }

        for (std::size_t row = 0; row < 25; row += 5) {
            for (std::size_t i = 0; i < 5; ++i) column[i] = state[row + i];
            for (std::size_t i = 0; i < 5; ++i) {
                state[row + i] = column[i] ^ ((~column[(i + 1) % 5]) & column[(i + 2) % 5]);
            }
        }
        state[0] ^= kRoundConstants[round];
    }
}

int hex_value(unsigned char c) noexcept {
    if (c >= '0' && c <= '9') return static_cast<int>(c - '0');
    if (c >= 'a' && c <= 'f') return static_cast<int>(c - 'a') + 10;
    if (c >= 'A' && c <= 'F') return static_cast<int>(c - 'A') + 10;
    return -1;
}

std::string_view strip_0x(std::string_view value) noexcept {
    if (value.size() >= 2 && value[0] == '0' && (value[1] == 'x' || value[1] == 'X')) return value.substr(2);
    return value;
}

bool parse_address(std::string_view value, std::array<std::uint8_t, 32>& word) noexcept {
    value = strip_0x(value);
    if (value.size() != 40) return false;
    word.fill(0);
    for (std::size_t i = 0; i < 20; ++i) {
        const int hi = hex_value(static_cast<unsigned char>(value[2 * i]));
        const int lo = hex_value(static_cast<unsigned char>(value[2 * i + 1]));
        if (hi < 0 || lo < 0) return false;
        word[12 + i] = static_cast<std::uint8_t>((hi << 4) | lo);
    }
    return true;
}

bool parse_bytes32(std::string_view value, std::array<std::uint8_t, 32>& word) noexcept {
    value = strip_0x(value);
    if (value.size() > 64) return false;
    word.fill(0);
    if (value.empty()) return true;
    const std::size_t nibble_offset = 64 - value.size();
    for (std::size_t i = 0; i < value.size(); ++i) {
        const int nibble = hex_value(static_cast<unsigned char>(value[i]));
        if (nibble < 0) return false;
        const std::size_t absolute = nibble_offset + i;
        const std::size_t byte_index = absolute / 2;
        if ((absolute & 1U) == 0) word[byte_index] |= static_cast<std::uint8_t>(nibble << 4);
        else word[byte_index] |= static_cast<std::uint8_t>(nibble);
    }
    return true;
}

bool parse_uint256_decimal(std::string_view value, std::array<std::uint8_t, 32>& word) noexcept {
    if (value.empty()) return false;
    word.fill(0);
    for (const unsigned char c : value) {
        if (c < '0' || c > '9') return false;
        unsigned carry = static_cast<unsigned>(c - '0');
        for (std::size_t pos = 32; pos-- > 0;) {
            const unsigned next = static_cast<unsigned>(word[pos]) * 10U + carry;
            word[pos] = static_cast<std::uint8_t>(next & 0xffU);
            carry = next >> 8U;
        }
        if (carry != 0) return false;
    }
    return true;
}

void encode_uint64(std::uint64_t value, std::array<std::uint8_t, 32>& word) noexcept {
    word.fill(0);
    for (std::size_t i = 0; i < 8; ++i) {
        word[31 - i] = static_cast<std::uint8_t>(value & 0xffU);
        value >>= 8U;
    }
}

void encode_uint8(std::uint8_t value, std::array<std::uint8_t, 32>& word) noexcept {
    word.fill(0);
    word[31] = value;
}

void patch_uint64_word(std::span<std::uint8_t> encoded, std::size_t index,
                       std::uint64_t value) noexcept {
    auto* dst = encoded.data() + index * 32 + 24;
    for (std::size_t i = 0; i < 8; ++i) {
        dst[7 - i] = static_cast<std::uint8_t>(value & 0xffU);
        value >>= 8U;
    }
}

void put_word(std::span<std::uint8_t> encoded, std::size_t index,
              const std::array<std::uint8_t, 32>& word) noexcept {
    std::memcpy(encoded.data() + index * 32, word.data(), word.size());
}

constexpr Hash32 kDomainTypeHash = Hash32{0x8bU, 0x73U, 0xc3U, 0xc6U, 0x9bU, 0xb8U, 0xfeU, 0x3dU, 0x51U, 0x2eU, 0xccU, 0x4cU, 0xf7U, 0x59U, 0xccU, 0x79U, 0x23U, 0x9fU, 0x7bU, 0x17U, 0x9bU, 0x0fU, 0xfaU, 0xcaU, 0xa9U, 0xa7U, 0x5dU, 0x52U, 0x2bU, 0x39U, 0x40U, 0x0fU};
constexpr Hash32 kOrderTypeHash = Hash32{0xbbU, 0x86U, 0x31U, 0x8aU, 0x21U, 0x38U, 0xf5U, 0xfaU, 0x8aU, 0xe3U, 0x2fU, 0xbeU, 0x8eU, 0x65U, 0x9fU, 0x8fU, 0xcfU, 0x13U, 0xccU, 0x6aU, 0xe4U, 0x01U, 0x4aU, 0x70U, 0x78U, 0x93U, 0x05U, 0x54U, 0x33U, 0x81U, 0x85U, 0x89U};
constexpr Hash32 kDomainNameHash = Hash32{0xf3U, 0x00U, 0x41U, 0xe9U, 0xaaU, 0xc4U, 0xc4U, 0xd3U, 0xa1U, 0x48U, 0x1dU, 0x29U, 0x41U, 0xdfU, 0xb0U, 0xa8U, 0x44U, 0xa7U, 0x20U, 0x40U, 0xe9U, 0xbbU, 0xc7U, 0x9aU, 0x81U, 0x0dU, 0x1eU, 0xc5U, 0xb5U, 0xd6U, 0xc7U, 0xafU};
constexpr Hash32 kDomainVersionHash = Hash32{0xadU, 0x7cU, 0x5bU, 0xefU, 0x02U, 0x78U, 0x16U, 0xa8U, 0x00U, 0xdaU, 0x17U, 0x36U, 0x44U, 0x4fU, 0xb5U, 0x8aU, 0x80U, 0x7eU, 0xf4U, 0xc9U, 0x60U, 0x3bU, 0x78U, 0x48U, 0x67U, 0x3fU, 0x7eU, 0x3aU, 0x68U, 0xebU, 0x14U, 0xa5U};

} // namespace

Hash32 keccak256(std::span<const std::uint8_t> input) noexcept {
    std::array<std::uint64_t, 25> state{};
    while (input.size() >= kKeccakRate) {
        for (std::size_t lane = 0; lane < kKeccakRate / 8; ++lane) {
            state[lane] ^= load64_le(input.data() + lane * 8);
        }
        keccak_f1600(state);
        input = input.subspan(kKeccakRate);
    }

    std::array<std::uint8_t, kKeccakRate> final_block{};
    if (!input.empty()) std::memcpy(final_block.data(), input.data(), input.size());
    final_block[input.size()] ^= 0x01U; // Ethereum Keccak domain suffix.
    final_block[kKeccakRate - 1] ^= 0x80U;
    for (std::size_t lane = 0; lane < kKeccakRate / 8; ++lane) {
        state[lane] ^= load64_le(final_block.data() + lane * 8);
    }
    keccak_f1600(state);

    Hash32 output{};
    for (std::size_t lane = 0; lane < output.size() / 8; ++lane) {
        store64_le(state[lane], output.data() + lane * 8);
    }
    return output;
}

Hash32 keccak256(std::string_view input) noexcept {
    return keccak256(std::span<const std::uint8_t>(
        reinterpret_cast<const std::uint8_t*>(input.data()), input.size()));
}

bool exchange_v2_domain_separator(const ExchangeV2DomainView& domain, Hash32& output) noexcept {
    if (domain.chain_id == 0 || domain.verifying_contract.empty()) return false;
    std::array<std::uint8_t, 5 * 32> encoded{};
    std::array<std::uint8_t, 32> word{};
    put_word(encoded, 0, kDomainTypeHash);
    put_word(encoded, 1, kDomainNameHash);
    put_word(encoded, 2, kDomainVersionHash);
    encode_uint64(domain.chain_id, word); put_word(encoded, 3, word);
    if (!parse_address(domain.verifying_contract, word)) return false;
    put_word(encoded, 4, word);
    output = keccak256(encoded);
    return true;
}

bool exchange_v2_order_struct_hash(const ExchangeV2OrderView& order, Hash32& output) noexcept {
    if (order.side > 1 || order.signature_type > 3) return false;
    std::array<std::uint8_t, 12 * 32> encoded{};
    std::array<std::uint8_t, 32> word{};
    put_word(encoded, 0, kOrderTypeHash);
    if (!parse_uint256_decimal(order.salt_decimal, word)) return false; put_word(encoded, 1, word);
    if (!parse_address(order.maker, word)) return false; put_word(encoded, 2, word);
    if (!parse_address(order.signer, word)) return false; put_word(encoded, 3, word);
    if (!parse_uint256_decimal(order.token_id_decimal, word)) return false; put_word(encoded, 4, word);
    if (!parse_uint256_decimal(order.maker_amount_decimal, word)) return false; put_word(encoded, 5, word);
    if (!parse_uint256_decimal(order.taker_amount_decimal, word)) return false; put_word(encoded, 6, word);
    encode_uint8(order.side, word); put_word(encoded, 7, word);
    encode_uint8(order.signature_type, word); put_word(encoded, 8, word);
    if (!parse_uint256_decimal(order.timestamp_decimal, word)) return false; put_word(encoded, 9, word);
    if (!parse_bytes32(order.metadata_hex, word)) return false; put_word(encoded, 10, word);
    if (!parse_bytes32(order.builder_hex, word)) return false; put_word(encoded, 11, word);
    output = keccak256(encoded);
    return true;
}

bool exchange_v2_order_digest(const ExchangeV2DomainView& domain,
                              const ExchangeV2OrderView& order,
                              Hash32& output) noexcept {
    ExchangeV2OrderHasher hasher(domain);
    return hasher.valid() && hasher.digest(order, output);
}

ExchangeV2OrderHasher::ExchangeV2OrderHasher(const ExchangeV2DomainView& domain) noexcept
    : valid_(exchange_v2_domain_separator(domain, domain_separator_)) {}

bool ExchangeV2OrderHasher::digest(const ExchangeV2OrderView& order, Hash32& output) const noexcept {
    if (!valid_) return false;
    Hash32 struct_hash{};
    if (!exchange_v2_order_struct_hash(order, struct_hash)) return false;
    std::array<std::uint8_t, 66> envelope{};
    envelope[0] = 0x19U;
    envelope[1] = 0x01U;
    std::memcpy(envelope.data() + 2, domain_separator_.data(), domain_separator_.size());
    std::memcpy(envelope.data() + 34, struct_hash.data(), struct_hash.size());
    output = keccak256(envelope);
    return true;
}


ExchangeV2PreparedOrderHasher::ExchangeV2PreparedOrderHasher(
    const ExchangeV2DomainView& domain,
    const ExchangeV2PreparedStaticView& fixed) noexcept {
    if (fixed.side > 1 || fixed.signature_type > 3) return;
    if (!exchange_v2_domain_separator(domain, domain_separator_)) return;
    std::array<std::uint8_t, 32> word{};
    put_word(encoded_, 0, kOrderTypeHash);
    if (!parse_address(fixed.maker, word)) return;
    put_word(encoded_, 2, word);
    if (!parse_address(fixed.signer, word)) return;
    put_word(encoded_, 3, word);
    if (!parse_uint256_decimal(fixed.token_id_decimal, word)) return;
    put_word(encoded_, 4, word);
    encode_uint8(fixed.side, word);
    put_word(encoded_, 7, word);
    encode_uint8(fixed.signature_type, word);
    put_word(encoded_, 8, word);
    if (!parse_bytes32(fixed.metadata_hex, word)) return;
    put_word(encoded_, 10, word);
    if (!parse_bytes32(fixed.builder_hex, word)) return;
    put_word(encoded_, 11, word);
    envelope_[0] = 0x19U;
    envelope_[1] = 0x01U;
    std::memcpy(envelope_.data() + 2, domain_separator_.data(), domain_separator_.size());
    valid_ = true;
}

bool ExchangeV2PreparedOrderHasher::digest_u64(
    std::uint64_t salt,
    std::uint64_t maker_amount,
    std::uint64_t taker_amount,
    std::uint64_t timestamp_ms,
    Hash32& output) noexcept {
    if (!valid_) return false;
    patch_uint64_word(encoded_, 1, salt);
    patch_uint64_word(encoded_, 5, maker_amount);
    patch_uint64_word(encoded_, 6, taker_amount);
    patch_uint64_word(encoded_, 9, timestamp_ms);

    const Hash32 struct_hash = keccak256(encoded_);
    std::memcpy(envelope_.data() + 34, struct_hash.data(), struct_hash.size());
    output = keccak256(envelope_);
    return true;
}

bool hash32_hex(const Hash32& hash, std::span<char> output) noexcept {
    static constexpr char kHex[] = "0123456789abcdef";
    if (output.size() < 64) return false;
    for (std::size_t i = 0; i < hash.size(); ++i) {
        output[2 * i] = kHex[hash[i] >> 4U];
        output[2 * i + 1] = kHex[hash[i] & 0x0fU];
    }
    return true;
}

} // namespace pm::v7::clob_eip712
