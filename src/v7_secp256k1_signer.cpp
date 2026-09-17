#include "pm/v7_secp256k1_signer.hpp"
#include <secp256k1.h>
#include <secp256k1_recovery.h>
#include <array>
#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <cstdlib>
#if defined(__linux__)
#include <sys/random.h>
#endif
namespace pm::v7::clob_signing {
namespace {
void secure_zero(std::span<std::uint8_t> bytes) noexcept {
 volatile std::uint8_t* p=bytes.data();
 for(std::size_t i=0;i<bytes.size();++i) p[i]=0;
}
bool random32(std::array<std::uint8_t,32>& output) noexcept {
#if defined(__APPLE__)
 arc4random_buf(output.data(),output.size()); return true;
#elif defined(__linux__)
 std::size_t done=0;
 while(done<output.size()){
  const auto rc=::getrandom(output.data()+done,output.size()-done,0);
  if(rc>0){ done+=static_cast<std::size_t>(rc); continue; }
  if(rc<0 && errno==EINTR) continue;
  return false;
 }
 return true;
#else
 (void)output; return false;
#endif
}
} // namespace
RecoverableSecp256k1Signer::RecoverableSecp256k1Signer(
 std::span<const std::uint8_t,32> secret_key) noexcept {
 context_=secp256k1_context_create(SECP256K1_CONTEXT_SIGN|SECP256K1_CONTEXT_VERIFY);
 if(context_==nullptr) return;
 std::array<std::uint8_t,32> blind{};
 if(!random32(blind) || secp256k1_context_randomize(context_,blind.data())!=1){
  secure_zero(blind); secp256k1_context_destroy(context_); context_=nullptr; return;
 }
 secure_zero(blind);
 if(secp256k1_ec_seckey_verify(context_,secret_key.data())!=1){
  secp256k1_context_destroy(context_); context_=nullptr; return;
 }
 std::memcpy(secret_key_.data(),secret_key.data(),secret_key_.size()); valid_=true;
}
RecoverableSecp256k1Signer::~RecoverableSecp256k1Signer(){
 secure_zero(secret_key_); if(context_!=nullptr) secp256k1_context_destroy(context_);
 context_=nullptr; valid_=false;
}
bool RecoverableSecp256k1Signer::sign_digest(
 std::span<const std::uint8_t,32> digest, RecoverableSignature65& output) const noexcept {
 if(!valid_ || context_==nullptr) return false;
 secp256k1_ecdsa_recoverable_signature sig{};
 if(secp256k1_ecdsa_sign_recoverable(context_,&sig,digest.data(),secret_key_.data(),nullptr,nullptr)!=1) return false;
 std::array<unsigned char,64> compact{}; int recid=-1;
 if(secp256k1_ecdsa_recoverable_signature_serialize_compact(context_,compact.data(),&recid,&sig)!=1) return false;
 if(recid<0 || recid>1) return false;
 std::memcpy(output.bytes.data(),compact.data(),compact.size());
 output.bytes[64]=static_cast<std::uint8_t>(27+recid); return true;
}
bool signature_hex_0x(const RecoverableSignature65& signature,std::span<char> output) noexcept {
 if(output.size()<132) return false;
 static constexpr char hex[]="0123456789abcdef";
 output[0]='0'; output[1]='x';
 for(std::size_t i=0;i<signature.bytes.size();++i){ output[2+i*2]=hex[signature.bytes[i]>>4U]; output[3+i*2]=hex[signature.bytes[i]&0x0fU]; }
 return true;
}
} // namespace pm::v7::clob_signing
