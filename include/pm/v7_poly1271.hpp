#pragma once

#include "pm/v7_clob_eip712.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <span>
#include <string_view>

namespace pm::v7::poly1271 {

using clob_eip712::ExchangeV2DomainView;
using clob_eip712::ExchangeV2OrderView;
using clob_eip712::Hash32;

inline constexpr std::size_t kEvmSignatureBytes = 65;
inline constexpr std::size_t kOrderTypeBytes = 186;
inline constexpr std::size_t kWrappedSignatureBytes =
    kEvmSignatureBytes + 32 + 32 + kOrderTypeBytes + 2;
inline constexpr std::size_t kWrappedSignatureHexChars = 2 + 2 * kWrappedSignatureBytes;

class Poly1271OrderHasher final {
public:
    Poly1271OrderHasher(const ExchangeV2DomainView& domain,
                       std::string_view deposit_wallet) noexcept;
    [[nodiscard]] bool valid() const noexcept { return valid_; }
    [[nodiscard]] const Hash32& domain_separator() const noexcept {
        return domain_separator_;
    }
    [[nodiscard]] bool digest(const ExchangeV2OrderView& order,
                              Hash32& output,
                              Hash32* contents_hash = nullptr) noexcept;
    [[nodiscard]] std::size_t wrap_signature(
        std::span<const std::uint8_t, kEvmSignatureBytes> signature,
        const Hash32& contents_hash,
        std::span<std::uint8_t> output) const noexcept;
    [[nodiscard]] std::size_t wrap_signature_hex(
        std::span<const std::uint8_t, kEvmSignatureBytes> signature,
        const Hash32& contents_hash,
        std::span<char> output) const noexcept;

private:
    Hash32 domain_separator_{};
    std::array<std::uint8_t, 32> deposit_wallet_word_{};
    std::array<std::uint8_t, 7 * 32> encoded_{};
    std::array<std::uint8_t, 66> envelope_{};
    bool valid_ = false;
};

// Compatibility boundary already published on main: prepared Solady / DepositWallet
// typed-data digest when the Exchange V2 app-domain separator is precomputed.
class PreparedHasher final {
public:
    PreparedHasher(std::uint64_t chain_id, std::string_view order_signer,
                   const Hash32& app_domain_separator) noexcept;
    [[nodiscard]] bool valid() const noexcept { return valid_; }
    [[nodiscard]] bool digest(const Hash32& contents_hash, Hash32& output) noexcept;
private:
    std::array<std::uint8_t, 7 * 32> encoded_{};
    std::array<std::uint8_t, 66> envelope_{};
    bool valid_ = false;
};

[[nodiscard]] bool wrap_signature_hex(
    std::span<const std::uint8_t, kEvmSignatureBytes> inner_signature,
    const Hash32& app_domain_separator,
    const Hash32& contents_hash,
    std::span<char> output) noexcept;

class Secp256k1Signer final {
public:
    explicit Secp256k1Signer(
        std::span<const std::uint8_t, 32> private_key) noexcept;
    ~Secp256k1Signer();
    Secp256k1Signer(const Secp256k1Signer&) = delete;
    Secp256k1Signer& operator=(const Secp256k1Signer&) = delete;
    Secp256k1Signer(Secp256k1Signer&&) = delete;
    Secp256k1Signer& operator=(Secp256k1Signer&&) = delete;

    [[nodiscard]] bool valid() const noexcept;
    // After cold-path construction/randomization the signer is immutable.
    // libsecp256k1 permits concurrent use of a constructed context for const
    // operations, so independent order lanes may share this signer.
    [[nodiscard]] bool sign_digest(
        const Hash32& digest,
        std::span<std::uint8_t, kEvmSignatureBytes> output) const noexcept;
    [[nodiscard]] bool address_hex(std::span<char> output) const noexcept;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

[[nodiscard]] bool sign_poly1271_hex(
    Poly1271OrderHasher& hasher,
    const Secp256k1Signer& signer,
    const ExchangeV2OrderView& order,
    std::span<char> output) noexcept;

} // namespace pm::v7::poly1271
