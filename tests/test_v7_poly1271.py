from __future__ import annotations
import shutil, subprocess, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
INNER=[163,160,147,200,59,108,32,200,51,85,193,108,233,76,146,230,233,252,189,235,132,6,24,204,116,246,197,122,66,173,20,91,43,152,219,115,210,199,60,191,31,43,106,242,136,86,106,232,25,96,221,188,58,19,146,16,39,53,138,139,255,59,230,255,28]
DOMAIN='a440cbd865bc0c6243d7a8df9a8bf48a8827b0a4abbb61c30e96d305423af148'
CONTENTS='d23d42d3ad94e65d78258cecaf8dcbaddac0f73dc085040f2c12bb595dd83804'
DIGEST='c31d535831b64decf4687570628e5765c218fc3c4a7b582c7bd3105525f76a8c'
ORDER_TYPE='Order(uint256 salt,address maker,address signer,uint256 tokenId,uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,uint256 timestamp,bytes32 metadata,bytes32 builder)'
PROGRAM=r'''
#include "pm/v7_poly1271.hpp"
#include <array>
#include <cstdio>
#include <string_view>
using namespace pm::v7;
int hv(char c){if(c>='0'&&c<='9')return c-'0';if(c>='a'&&c<='f')return c-'a'+10;if(c>='A'&&c<='F')return c-'A'+10;return -1;}
clob_eip712::Hash32 parse(std::string_view s){clob_eip712::Hash32 x{};for(size_t i=0;i<32;++i)x[i]=(hv(s[i*2])<<4)|hv(s[i*2+1]);return x;}
int main(){
 auto domain=parse("a440cbd865bc0c6243d7a8df9a8bf48a8827b0a4abbb61c30e96d305423af148");
 auto contents=parse("d23d42d3ad94e65d78258cecaf8dcbaddac0f73dc085040f2c12bb595dd83804");
 poly1271::PreparedHasher h(80002,"0x1111111111111111111111111111111111111111",domain); if(!h.valid())return 2;
 clob_eip712::Hash32 digest{}; if(!h.digest(contents,digest))return 3;
 std::array<char,64> dhex{}; if(!clob_eip712::hash32_hex(digest,dhex))return 4; std::printf("%.*s\n",64,dhex.data());
 const std::array<std::uint8_t,65> inner={163,160,147,200,59,108,32,200,51,85,193,108,233,76,146,230,233,252,189,235,132,6,24,204,116,246,197,122,66,173,20,91,43,152,219,115,210,199,60,191,31,43,106,242,136,86,106,232,25,96,221,188,58,19,146,16,39,53,138,139,255,59,230,255,28};
 std::array<char,poly1271::kWrappedSignatureHexChars> out{}; if(!poly1271::wrap_signature_hex(inner,domain,contents,out))return 5; std::printf("%.*s\n",(int)out.size(),out.data());
 poly1271::PreparedHasher bad(80002,"0x1234",domain); if(bad.valid())return 6;
 return 0;
}
'''
def test_poly1271_digest_and_wrapper_match_official_v2_fixture():
    cxx=shutil.which('c++'); assert cxx
    with tempfile.TemporaryDirectory() as tmp:
        p=Path(tmp); src=p/'main.cpp'; binary=p/'poly1271-test'; src.write_text(PROGRAM)
        subprocess.run([cxx,'-std=c++20','-O2','-Wall','-Wextra','-Wpedantic',f'-I{ROOT/"include"}',
            str(ROOT/'src/v7_clob_eip712.cpp'),str(ROOT/'src/v7_keccak_fast.cpp'),str(ROOT/'src/v7_poly1271.cpp'),str(src),'-o',str(binary)],
            check=True,capture_output=True,text=True)
        lines=subprocess.run([str(binary)],check=True,capture_output=True,text=True).stdout.splitlines()
    assert lines[0]==DIGEST
    expected='0x'+bytes(INNER).hex()+DOMAIN+CONTENTS+ORDER_TYPE.encode().hex()+len(ORDER_TYPE).to_bytes(2,'big').hex()
    assert lines[1]==expected
    assert len(lines[1])==636
