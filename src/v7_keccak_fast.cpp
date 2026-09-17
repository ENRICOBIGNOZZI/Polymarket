#include "pm/v7_keccak_fast.hpp"

#include <array>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <cstring>

namespace pm::v7::clob_eip712 {
namespace {

constexpr std::size_t kRate = 136;
constexpr std::array<std::uint64_t, 24> kRc{
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

[[nodiscard]] std::uint64_t load64_le(const std::uint8_t* p) noexcept {
    std::uint64_t value = 0;
    if constexpr (std::endian::native == std::endian::little) {
        std::memcpy(&value, p, sizeof(value));
    } else {
        for (unsigned i = 0; i < 8; ++i) {
            value |= static_cast<std::uint64_t>(p[i]) << (8U * i);
        }
    }
    return value;
}

void store64_le(std::uint64_t value, std::uint8_t* p) noexcept {
    if constexpr (std::endian::native == std::endian::little) {
        std::memcpy(p, &value, sizeof(value));
    } else {
        for (unsigned i = 0; i < 8; ++i) {
            p[i] = static_cast<std::uint8_t>(value >> (8U * i));
        }
    }
}

void keccak_f1600_unrolled(std::array<std::uint64_t, 25>& s) noexcept {
    for (std::size_t round = 0; round < kRc.size(); ++round) {
        const std::uint64_t c0 = s[0] ^ s[5] ^ s[10] ^ s[15] ^ s[20];
        const std::uint64_t c1 = s[1] ^ s[6] ^ s[11] ^ s[16] ^ s[21];
        const std::uint64_t c2 = s[2] ^ s[7] ^ s[12] ^ s[17] ^ s[22];
        const std::uint64_t c3 = s[3] ^ s[8] ^ s[13] ^ s[18] ^ s[23];
        const std::uint64_t c4 = s[4] ^ s[9] ^ s[14] ^ s[19] ^ s[24];

        const std::uint64_t d0 = c4 ^ std::rotl(c1, 1);
        const std::uint64_t d1 = c0 ^ std::rotl(c2, 1);
        const std::uint64_t d2 = c1 ^ std::rotl(c3, 1);
        const std::uint64_t d3 = c2 ^ std::rotl(c4, 1);
        const std::uint64_t d4 = c3 ^ std::rotl(c0, 1);

        s[0] ^= d0; s[5] ^= d0; s[10] ^= d0; s[15] ^= d0; s[20] ^= d0;
        s[1] ^= d1; s[6] ^= d1; s[11] ^= d1; s[16] ^= d1; s[21] ^= d1;
        s[2] ^= d2; s[7] ^= d2; s[12] ^= d2; s[17] ^= d2; s[22] ^= d2;
        s[3] ^= d3; s[8] ^= d3; s[13] ^= d3; s[18] ^= d3; s[23] ^= d3;
        s[4] ^= d4; s[9] ^= d4; s[14] ^= d4; s[19] ^= d4; s[24] ^= d4;

        std::uint64_t t = s[1];
        std::uint64_t saved = 0;
#define PM_KECCAK_MOVE(LANE, ROT) \
        do { saved = s[LANE]; s[LANE] = std::rotl(t, ROT); t = saved; } while (false)
        PM_KECCAK_MOVE(10, 1);
        PM_KECCAK_MOVE(7, 3);
        PM_KECCAK_MOVE(11, 6);
        PM_KECCAK_MOVE(17, 10);
        PM_KECCAK_MOVE(18, 15);
        PM_KECCAK_MOVE(3, 21);
        PM_KECCAK_MOVE(5, 28);
        PM_KECCAK_MOVE(16, 36);
        PM_KECCAK_MOVE(8, 45);
        PM_KECCAK_MOVE(21, 55);
        PM_KECCAK_MOVE(24, 2);
        PM_KECCAK_MOVE(4, 14);
        PM_KECCAK_MOVE(15, 27);
        PM_KECCAK_MOVE(23, 41);
        PM_KECCAK_MOVE(19, 56);
        PM_KECCAK_MOVE(13, 8);
        PM_KECCAK_MOVE(12, 25);
        PM_KECCAK_MOVE(2, 43);
        PM_KECCAK_MOVE(20, 62);
        PM_KECCAK_MOVE(14, 18);
        PM_KECCAK_MOVE(22, 39);
        PM_KECCAK_MOVE(9, 61);
        PM_KECCAK_MOVE(6, 20);
        PM_KECCAK_MOVE(1, 44);
#undef PM_KECCAK_MOVE

#define PM_KECCAK_CHI(BASE) \
        do { \
            const std::uint64_t a0 = s[(BASE) + 0]; \
            const std::uint64_t a1 = s[(BASE) + 1]; \
            const std::uint64_t a2 = s[(BASE) + 2]; \
            const std::uint64_t a3 = s[(BASE) + 3]; \
            const std::uint64_t a4 = s[(BASE) + 4]; \
            s[(BASE) + 0] = a0 ^ ((~a1) & a2); \
            s[(BASE) + 1] = a1 ^ ((~a2) & a3); \
            s[(BASE) + 2] = a2 ^ ((~a3) & a4); \
            s[(BASE) + 3] = a3 ^ ((~a4) & a0); \
            s[(BASE) + 4] = a4 ^ ((~a0) & a1); \
        } while (false)
        PM_KECCAK_CHI(0);
        PM_KECCAK_CHI(5);
        PM_KECCAK_CHI(10);
        PM_KECCAK_CHI(15);
        PM_KECCAK_CHI(20);
#undef PM_KECCAK_CHI

        s[0] ^= kRc[round];
    }
}

} // namespace

Hash32 keccak256_unrolled(std::span<const std::uint8_t> input) noexcept {
    std::array<std::uint64_t, 25> state{};
    while (input.size() >= kRate) {
        for (std::size_t lane = 0; lane < kRate / 8; ++lane) {
            state[lane] ^= load64_le(input.data() + lane * 8);
        }
        keccak_f1600_unrolled(state);
        input = input.subspan(kRate);
    }

    std::array<std::uint8_t, kRate> final_block{};
    if (!input.empty()) std::memcpy(final_block.data(), input.data(), input.size());
    final_block[input.size()] ^= 0x01U;
    final_block[kRate - 1] ^= 0x80U;
    for (std::size_t lane = 0; lane < kRate / 8; ++lane) {
        state[lane] ^= load64_le(final_block.data() + lane * 8);
    }
    keccak_f1600_unrolled(state);

    Hash32 output{};
    for (std::size_t lane = 0; lane < output.size() / 8; ++lane) {
        store64_le(state[lane], output.data() + lane * 8);
    }
    return output;
}

} // namespace pm::v7::clob_eip712
