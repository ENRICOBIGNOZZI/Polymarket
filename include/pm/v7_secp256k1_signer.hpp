#pragma once
#include <array>
#include <cstddef>
#include <cstdint>
#include <span>
struct secp256k1_context_struct;
namespace pm::v7::clob_signing {
struct RecoverableSignature65 { std::array<std::uint8_t,65> bytes{}; };
class RecoverableSecp256k1Signer final {
public:
 explicit RecoverableSecp256k1Signer(std::span<const std::uint8_t,32> secret_key) noexcept;
 ~RecoverableSecp256k1Signer();
 RecoverableSecp256k1Signer(const RecoverableSecp256k1Signer&)=delete;
 RecoverableSecp256k1Signer& operator=(const RecoverableSecp256k1Signer&)=delete;
 RecoverableSecp256k1Signer(RecoverableSecp256k1Signer&&)=delete;
 RecoverableSecp256k1Signer& operator=(RecoverableSecp256k1Signer&&)=delete;
 [[nodiscard]] bool valid() const noexcept { return valid_; }
 [[nodiscard]] bool sign_digest(std::span<const std::uint8_t,32> digest,
                                RecoverableSignature65& output) const noexcept;
private:
 secp256k1_context_struct* context_=nullptr;
 std::array<std::uint8_t,32> secret_key_{};
 bool valid_=false;
};
[[nodiscard]] bool signature_hex_0x(const RecoverableSignature65& signature,
                                    std::span<char> output) noexcept;
} // namespace pm::v7::clob_signing
