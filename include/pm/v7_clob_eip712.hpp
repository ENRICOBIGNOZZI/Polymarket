#pragma once

#include <array>
#include <cstdint>
#include <span>
#include <string_view>

namespace pm::v7::clob_eip712 {

using Hash32 = std::array<std::uint8_t, 32>;

struct ExchangeV2DomainView {
    std::uint64_t chain_id = 0;
    std::string_view verifying_contract;
};

struct ExchangeV2OrderView {
    std::string_view salt_decimal;
    std::string_view maker;
    std::string_view signer;
    std::string_view token_id_decimal;
    std::string_view maker_amount_decimal;
    std::string_view taker_amount_decimal;
    std::uint8_t side = 0;
    std::uint8_t signature_type = 0;
    std::string_view timestamp_decimal;
    std::string_view metadata_hex;
    std::string_view builder_hex;
};

// Ethereum Keccak-256, not NIST SHA3-256.
[[nodiscard]] Hash32 keccak256(std::span<const std::uint8_t> input) noexcept;
[[nodiscard]] Hash32 keccak256(std::string_view input) noexcept;

// Current CLOB V2 domain:
// EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)
// name="Polymarket CTF Exchange", version="2".
[[nodiscard]] bool exchange_v2_domain_separator(
    const ExchangeV2DomainView& domain, Hash32& output) noexcept;

// Current CLOB V2 Order struct hash. `expiration` is not part of the EIP-712
// signed struct in the official V2 contract and is therefore intentionally
// absent from ExchangeV2OrderView.
[[nodiscard]] bool exchange_v2_order_struct_hash(
    const ExchangeV2OrderView& order, Hash32& output) noexcept;

// Standard EIP-712 digest: keccak256(0x1901 || domain_separator || struct_hash).
// This is the digest directly signed by EOA / proxy / Safe order-signature
// flows. Deposit-wallet POLY_1271 wrapping is a separate protocol stage.
[[nodiscard]] bool exchange_v2_order_digest(
    const ExchangeV2DomainView& domain,
    const ExchangeV2OrderView& order,
    Hash32& output) noexcept;

// Hot-path form: compute the domain separator once at startup, then hash only
// the variable Order struct plus the 66-byte EIP-712 envelope per decision.
class ExchangeV2OrderHasher final {
public:
    explicit ExchangeV2OrderHasher(const ExchangeV2DomainView& domain) noexcept;
    [[nodiscard]] bool valid() const noexcept { return valid_; }
    [[nodiscard]] const Hash32& domain_separator() const noexcept { return domain_separator_; }
    [[nodiscard]] bool digest(const ExchangeV2OrderView& order, Hash32& output) const noexcept;
private:
    Hash32 domain_separator_{};
    bool valid_ = false;
};


// Static portion of one Exchange V2 order lane. For one market/account these
// fields do not change per decision and should not be reparsed in the hot path.
struct ExchangeV2PreparedStaticView {
    std::string_view maker;
    std::string_view signer;
    std::string_view token_id_decimal;
    std::uint8_t side = 0;
    std::uint8_t signature_type = 0;
    std::string_view metadata_hex;
    std::string_view builder_hex;
};

// Single-owner hot-path hasher. Construction parses/ABI-encodes every static
// field once. digest_u64() patches only salt, maker/taker amount and timestamp,
// then performs the Order Keccak plus final 66-byte EIP-712 envelope Keccak.
// The official V2 salt generator, millisecond timestamp and CLOB 6-decimal
// amounts all fit uint64; tokenId remains pre-parsed as uint256 at construction.
class ExchangeV2PreparedOrderHasher final {
public:
    ExchangeV2PreparedOrderHasher(const ExchangeV2DomainView& domain,
                                  const ExchangeV2PreparedStaticView& fixed) noexcept;
    [[nodiscard]] bool valid() const noexcept { return valid_; }
    [[nodiscard]] const Hash32& domain_separator() const noexcept { return domain_separator_; }
    [[nodiscard]] bool digest_u64(std::uint64_t salt,
                                  std::uint64_t maker_amount,
                                  std::uint64_t taker_amount,
                                  std::uint64_t timestamp_ms,
                                  Hash32& output) noexcept;
private:
    Hash32 domain_separator_{};
    std::array<std::uint8_t, 12 * 32> encoded_{};
    std::array<std::uint8_t, 66> envelope_{};
    bool valid_ = false;
};

[[nodiscard]] bool hash32_hex(const Hash32& hash, std::span<char> output) noexcept;

} // namespace pm::v7::clob_eip712
