#include "pm/v7_poly1271.hpp"
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
namespace pm::v7::poly1271 {
namespace {
constexpr Hash32 kSoladyTypeHash{0x6b,0xa0,0x28,0x56,0x5c,0xb3,0x24,0xc2,0xaa,0x02,0xbb,0x71,0x4b,0x98,0x16,0xd0,0xbd,0xdd,0x55,0x7a,0x2f,0x33,0xbb,0x36,0xcf,0x13,0x27,0x2a,0x42,0x56,0xbd,0x42};
constexpr Hash32 kDepositNameHash{0xd6,0x82,0xb5,0x29,0xa1,0x7c,0xda,0x19,0xaa,0x27,0x5f,0x3a,0x05,0x06,0x08,0xf9,0xe9,0x40,0x1f,0xad,0xd1,0xb0,0xd2,0x33,0xd8,0x15,0x19,0x97,0x22,0x95,0x82,0x8b};
constexpr Hash32 kDepositVersionHash{0xc8,0x9e,0xfd,0xaa,0x54,0xc0,0xf2,0x0c,0x7a,0xdf,0x61,0x28,0x82,0xdf,0x09,0x50,0xf5,0xa9,0x51,0x63,0x7e,0x03,0x07,0xcd,0xcb,0x4c,0x67,0x2f,0x29,0x8b,0x8b,0xc6};
constexpr std::string_view kOrderType="Order(uint256 salt,address maker,address signer,uint256 tokenId,uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,uint256 timestamp,bytes32 metadata,bytes32 builder)";
static_assert(kOrderType.size()==186);
int hex_value(char c) noexcept { if(c>='0'&&c<='9')return c-'0'; if(c>='a'&&c<='f')return c-'a'+10; if(c>='A'&&c<='F')return c-'A'+10; return -1; }
bool encode_address(std::string_view value,std::uint8_t* dst) noexcept {
 if(value.size()!=42||value[0]!='0'||(value[1]!='x'&&value[1]!='X'))return false;
 std::memset(dst,0,32);
 for(std::size_t i=0;i<20;++i){int h=hex_value(value[2+i*2]),l=hex_value(value[3+i*2]); if(h<0||l<0)return false; dst[12+i]=static_cast<std::uint8_t>((h<<4)|l);}
 return true;
}
void encode_u64(std::uint64_t v,std::uint8_t* dst) noexcept { std::memset(dst,0,32); for(std::size_t i=0;i<8;++i){dst[31-i]=static_cast<std::uint8_t>(v);v>>=8;} }
void write_hex(std::span<char> out,std::size_t offset,std::span<const std::uint8_t> in) noexcept {
 static constexpr char h[]="0123456789abcdef"; for(std::size_t i=0;i<in.size();++i){out[offset+i*2]=h[in[i]>>4U];out[offset+i*2+1]=h[in[i]&15U];}
}
}
PreparedHasher::PreparedHasher(std::uint64_t chain_id,std::string_view signer,const Hash32& domain) noexcept {
 if(chain_id==0)return;
 std::memcpy(encoded_.data()+0*32,kSoladyTypeHash.data(),32);
 std::memcpy(encoded_.data()+2*32,kDepositNameHash.data(),32);
 std::memcpy(encoded_.data()+3*32,kDepositVersionHash.data(),32);
 encode_u64(chain_id,encoded_.data()+4*32);
 if(!encode_address(signer,encoded_.data()+5*32))return;
 std::memset(encoded_.data()+6*32,0,32);
 envelope_[0]=0x19; envelope_[1]=0x01;
 std::memcpy(envelope_.data()+2,domain.data(),32);
 valid_=true;
}
bool PreparedHasher::digest(const Hash32& contents,Hash32& output) noexcept {
 if(!valid_)return false;
 std::memcpy(encoded_.data()+1*32,contents.data(),32);
 const auto typed=pm::v7::clob_eip712::keccak256(encoded_);
 std::memcpy(envelope_.data()+34,typed.data(),32);
 output=pm::v7::clob_eip712::keccak256(envelope_);
 return true;
}
bool wrap_signature_hex(std::span<const std::uint8_t,65> inner,const Hash32& domain,const Hash32& contents,std::span<char> out) noexcept {
 if(out.size()<kWrappedSignatureHexChars)return false;
 out[0]='0';out[1]='x'; std::size_t p=2;
 write_hex(out,p,inner); p+=130;
 write_hex(out,p,domain); p+=64;
 write_hex(out,p,contents); p+=64;
 write_hex(out,p,std::span<const std::uint8_t>(reinterpret_cast<const std::uint8_t*>(kOrderType.data()),kOrderType.size())); p+=kOrderType.size()*2;
 const std::array<std::uint8_t,2> len{static_cast<std::uint8_t>(kOrderType.size()>>8U),static_cast<std::uint8_t>(kOrderType.size())};
 write_hex(out,p,len); p+=4;
 return p==kWrappedSignatureHexChars;
}
} // namespace pm::v7::poly1271
