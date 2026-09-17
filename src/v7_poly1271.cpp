#include "pm/v7_poly1271.hpp"

#include <openssl/crypto.h>
#include <openssl/rand.h>
#include <secp256k1.h>
#include <secp256k1_recovery.h>

#include <algorithm>
#include <array>
#include <cstring>
#include <new>
#include <utility>

namespace pm::v7::poly1271 {
namespace {

constexpr std::string_view kOrderType =
    "Order(uint256 salt,address maker,address signer,uint256 tokenId,"
    "uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,"
    "uint256 timestamp,bytes32 metadata,bytes32 builder)";
static_assert(kOrderType.size() == kOrderTypeBytes);
constexpr std::string_view kTypedDataSignType =
    "TypedDataSign(Order contents,string name,string version,uint256 chainId,"
    "address verifyingContract,bytes32 salt)Order(uint256 salt,address maker,"
    "address signer,uint256 tokenId,uint256 makerAmount,uint256 takerAmount,"
    "uint8 side,uint8 signatureType,uint256 timestamp,bytes32 metadata,"
    "bytes32 builder)";
constexpr std::string_view kDepositWalletName = "DepositWallet";
constexpr std::string_view kDepositWalletVersion = "1";

int hex_value(unsigned char c) noexcept {
    if (c >= '0' && c <= '9') return static_cast<int>(c - '0');
    if (c >= 'a' && c <= 'f') return static_cast<int>(c - 'a') + 10;
    if (c >= 'A' && c <= 'F') return static_cast<int>(c - 'A') + 10;
    return -1;
}

std::string_view strip_0x(std::string_view value) noexcept {
    if (value.size() >= 2 && value[0] == '0'
        && (value[1] == 'x' || value[1] == 'X')) return value.substr(2);
    return value;
}
bool parse_address(std::string_view value,
                   std::array<std::uint8_t, 32>& word) noexcept {
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

void encode_uint64(std::uint64_t value,
                   std::array<std::uint8_t, 32>& word) noexcept {
    word.fill(0);
    for (std::size_t i = 0; i < 8; ++i) {
        word[31 - i] = static_cast<std::uint8_t>(value & 0xffU);
        value >>= 8U;
    }
}

void put_word(std::span<std::uint8_t> encoded, std::size_t index,
              const std::array<std::uint8_t, 32>& word) noexcept {
    std::memcpy(encoded.data() + index * 32, word.data(), word.size());
}
bool bytes_hex(std::span<const std::uint8_t> input,
               std::span<char> output,
               bool prefix) noexcept {
    static constexpr char kHex[] = "0123456789abcdef";
    const std::size_t required = 2 * input.size() + (prefix ? 2 : 0);
    if (output.size() < required) return false;
    std::size_t pos = 0;
    if (prefix) {
        output[pos++] = '0';
        output[pos++] = 'x';
    }
    for (const auto byte : input) {
        output[pos++] = kHex[byte >> 4U];
        output[pos++] = kHex[byte & 0x0fU];
    }
    return true;
}

void secure_zero(void* data, std::size_t size) noexcept {
    if (data != nullptr && size != 0) OPENSSL_cleanse(data, size);
}

} // namespace
Poly1271OrderHasher::Poly1271OrderHasher(
    const ExchangeV2DomainView& domain,
    std::string_view deposit_wallet) noexcept {
    if (!clob_eip712::exchange_v2_domain_separator(domain, domain_separator_)) return;
    if (!parse_address(deposit_wallet, deposit_wallet_word_)) return;

    const Hash32 type_hash = clob_eip712::keccak256(kTypedDataSignType);
    const Hash32 name_hash = clob_eip712::keccak256(kDepositWalletName);
    const Hash32 version_hash = clob_eip712::keccak256(kDepositWalletVersion);
    std::array<std::uint8_t, 32> word{};
    put_word(encoded_, 0, type_hash);
    put_word(encoded_, 2, name_hash);
    put_word(encoded_, 3, version_hash);
    encode_uint64(domain.chain_id, word);
    put_word(encoded_, 4, word);
    put_word(encoded_, 5, deposit_wallet_word_);
    // word 6 is the required bytes32-zero salt.
    envelope_[0] = 0x19U;
    envelope_[1] = 0x01U;
    std::memcpy(envelope_.data() + 2,
                domain_separator_.data(), domain_separator_.size());
    valid_ = true;
}
bool Poly1271OrderHasher::digest(const ExchangeV2OrderView& order,
                                 Hash32& output,
                                 Hash32* contents_hash) noexcept {
    if (!valid_ || order.signature_type != 3) return false;
    std::array<std::uint8_t, 32> signer_word{};
    if (!parse_address(order.signer, signer_word)
        || signer_word != deposit_wallet_word_) return false;

    Hash32 contents{};
    if (!clob_eip712::exchange_v2_order_struct_hash(order, contents)) return false;
    put_word(encoded_, 1, contents);
    const Hash32 struct_hash = clob_eip712::keccak256(encoded_);
    std::memcpy(envelope_.data() + 34, struct_hash.data(), struct_hash.size());
    output = clob_eip712::keccak256(envelope_);
    if (contents_hash != nullptr) *contents_hash = contents;
    return true;
}

std::size_t Poly1271OrderHasher::wrap_signature(
    std::span<const std::uint8_t, kEvmSignatureBytes> signature,
    const Hash32& contents_hash,
    std::span<std::uint8_t> output) const noexcept {
    if (!valid_ || output.size() < kWrappedSignatureBytes) return 0;
    std::size_t pos = 0;
    std::memcpy(output.data() + pos, signature.data(), signature.size());
    pos += signature.size();
    std::memcpy(output.data() + pos, domain_separator_.data(), domain_separator_.size());
    pos += domain_separator_.size();
    std::memcpy(output.data() + pos, contents_hash.data(), contents_hash.size());
    pos += contents_hash.size();
    std::memcpy(output.data() + pos, kOrderType.data(), kOrderType.size());
    pos += kOrderType.size();
    output[pos++] = static_cast<std::uint8_t>((kOrderType.size() >> 8U) & 0xffU);
    output[pos++] = static_cast<std::uint8_t>(kOrderType.size() & 0xffU);
    return pos;
}

std::size_t Poly1271OrderHasher::wrap_signature_hex(
    std::span<const std::uint8_t, kEvmSignatureBytes> signature,
    const Hash32& contents_hash,
    std::span<char> output) const noexcept {
    std::array<std::uint8_t, kWrappedSignatureBytes> wrapped{};
    const auto size = wrap_signature(signature, contents_hash, wrapped);
    if (size != wrapped.size() || output.size() < kWrappedSignatureHexChars) return 0;
    if (!bytes_hex(wrapped, output, true)) return 0;
    return kWrappedSignatureHexChars;
}

PreparedHasher::PreparedHasher(
    std::uint64_t chain_id, std::string_view order_signer,
    const Hash32& app_domain_separator) noexcept {
    if (chain_id == 0) return;
    const Hash32 type_hash = clob_eip712::keccak256(kTypedDataSignType);
    const Hash32 name_hash = clob_eip712::keccak256(kDepositWalletName);
    const Hash32 version_hash = clob_eip712::keccak256(kDepositWalletVersion);
    std::array<std::uint8_t, 32> word{};
    put_word(encoded_, 0, type_hash);
    put_word(encoded_, 2, name_hash);
    put_word(encoded_, 3, version_hash);
    encode_uint64(chain_id, word);
    put_word(encoded_, 4, word);
    if (!parse_address(order_signer, word)) return;
    put_word(encoded_, 5, word);
    envelope_[0] = 0x19U;
    envelope_[1] = 0x01U;
    std::memcpy(envelope_.data() + 2, app_domain_separator.data(), app_domain_separator.size());
    valid_ = true;
}

bool PreparedHasher::digest(const Hash32& contents_hash, Hash32& output) noexcept {
    if (!valid_) return false;
    put_word(encoded_, 1, contents_hash);
    const Hash32 typed_hash = clob_eip712::keccak256(encoded_);
    std::memcpy(envelope_.data() + 34, typed_hash.data(), typed_hash.size());
    output = clob_eip712::keccak256(envelope_);
    return true;
}

bool wrap_signature_hex(
    std::span<const std::uint8_t, kEvmSignatureBytes> inner_signature,
    const Hash32& app_domain_separator, const Hash32& contents_hash,
    std::span<char> output) noexcept {
    if (output.size() < kWrappedSignatureHexChars) return false;
    std::array<std::uint8_t, kWrappedSignatureBytes> wrapped{};
    std::size_t pos = 0;
    std::memcpy(wrapped.data() + pos, inner_signature.data(), inner_signature.size());
    pos += inner_signature.size();
    std::memcpy(wrapped.data() + pos, app_domain_separator.data(), app_domain_separator.size());
    pos += app_domain_separator.size();
    std::memcpy(wrapped.data() + pos, contents_hash.data(), contents_hash.size());
    pos += contents_hash.size();
    std::memcpy(wrapped.data() + pos, kOrderType.data(), kOrderType.size());
    pos += kOrderType.size();
    wrapped[pos++] = static_cast<std::uint8_t>((kOrderType.size() >> 8U) & 0xffU);
    wrapped[pos++] = static_cast<std::uint8_t>(kOrderType.size() & 0xffU);
    return pos == wrapped.size() && bytes_hex(wrapped, output, true);
}

struct Secp256k1Signer::Impl final {
    secp256k1_context* context = nullptr;
    std::array<std::uint8_t, 32> private_key{};
    std::array<std::uint8_t, 20> address{};
    bool valid = false;
    ~Impl() {
        if (context != nullptr) secp256k1_context_destroy(context);
        secure_zero(private_key.data(), private_key.size());
    }
};

Secp256k1Signer::Secp256k1Signer(
    std::span<const std::uint8_t, 32> private_key) noexcept
    : impl_(new (std::nothrow) Impl) {
    if (!impl_) return;
    auto& p = *impl_;
    std::copy(private_key.begin(), private_key.end(), p.private_key.begin());
    if (secp256k1_ec_seckey_verify(secp256k1_context_static,
                                   p.private_key.data()) != 1) return;
    p.context = secp256k1_context_create(SECP256K1_CONTEXT_NONE);
    if (p.context == nullptr) return;
    std::array<std::uint8_t, 32> seed{};
    if (RAND_bytes(seed.data(), static_cast<int>(seed.size())) != 1
        || secp256k1_context_randomize(p.context, seed.data()) != 1) {
        secure_zero(seed.data(), seed.size());
        return;
    }
    secure_zero(seed.data(), seed.size());
    secp256k1_pubkey public_key{};
    if (secp256k1_ec_pubkey_create(p.context, &public_key,
                                   p.private_key.data()) != 1) return;
    std::array<std::uint8_t, 65> serialized{};
    std::size_t serialized_size = serialized.size();
    if (secp256k1_ec_pubkey_serialize(
            p.context, serialized.data(), &serialized_size, &public_key,
            SECP256K1_EC_UNCOMPRESSED) != 1
        || serialized_size != serialized.size() || serialized[0] != 0x04U) return;
    const Hash32 public_hash = clob_eip712::keccak256(
        std::span<const std::uint8_t>(serialized.data() + 1, 64));
    std::copy(public_hash.end() - 20, public_hash.end(), p.address.begin());
    secure_zero(serialized.data(), serialized.size());
    p.valid = true;
}

Secp256k1Signer::~Secp256k1Signer() = default;

bool Secp256k1Signer::valid() const noexcept {
    return impl_ != nullptr && impl_->valid;
}

bool Secp256k1Signer::address_hex(std::span<char> output) const noexcept {
    if (!valid() || output.size() < 42) return false;
    return bytes_hex(impl_->address, output, true);
}

bool Secp256k1Signer::sign_digest(
    const Hash32& digest,
    std::span<std::uint8_t, kEvmSignatureBytes> output) const noexcept {
    if (!valid()) return false;
    secp256k1_ecdsa_recoverable_signature signature{};
    if (secp256k1_ecdsa_sign_recoverable(
            impl_->context, &signature, digest.data(), impl_->private_key.data(),
            nullptr, nullptr) != 1) return false;
    int recovery = -1;
    if (secp256k1_ecdsa_recoverable_signature_serialize_compact(
            impl_->context, output.data(), &recovery, &signature) != 1
        || recovery < 0 || recovery > 1) return false;
    output[64] = static_cast<std::uint8_t>(27 + recovery);
    return true;
}

bool sign_poly1271_hex(Poly1271OrderHasher& hasher,
                       const Secp256k1Signer& signer,
                       const ExchangeV2OrderView& order,
                       std::span<char> output) noexcept {
    if (!hasher.valid() || !signer.valid()
        || output.size() < kWrappedSignatureHexChars) return false;
    Hash32 digest{}, contents{};
    if (!hasher.digest(order, digest, &contents)) return false;
    std::array<std::uint8_t, kEvmSignatureBytes> signature{};
    if (!signer.sign_digest(digest, signature)) return false;
    const auto written = hasher.wrap_signature_hex(signature, contents, output);
    secure_zero(signature.data(), signature.size());
    secure_zero(digest.data(), digest.size());
    secure_zero(contents.data(), contents.size());
    return written == kWrappedSignatureHexChars;
}

bool sign_prepared_poly1271_hex(
    clob_eip712::ExchangeV2PreparedOrderHasher& order_hasher,
    PreparedHasher& poly_hasher,
    const Secp256k1Signer& signer,
    std::uint64_t salt,
    std::uint64_t maker_amount,
    std::uint64_t taker_amount,
    std::uint64_t timestamp_ms,
    std::span<char> output) noexcept {
    if (!order_hasher.valid() || !poly_hasher.valid() || !signer.valid()
        || output.size() < kWrappedSignatureHexChars) return false;
    Hash32 contents{}, digest{};
    std::array<std::uint8_t, kEvmSignatureBytes> signature{};
    const bool ok = order_hasher.struct_hash_u64(
            salt, maker_amount, taker_amount, timestamp_ms, contents)
        && poly_hasher.digest(contents, digest)
        && signer.sign_digest(digest, signature)
        && wrap_signature_hex(signature, order_hasher.domain_separator(), contents, output);
    secure_zero(signature.data(), signature.size());
    secure_zero(digest.data(), digest.size());
    secure_zero(contents.data(), contents.size());
    return ok;
}

} // namespace pm::v7::poly1271
