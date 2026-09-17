from __future__ import annotations
import shlex, shutil, subprocess, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
EXPECTED=[97,132,166,58,69,199,219,31,26,132,82,86,31,26,47,182,
152,102,30,235,30,28,66,151,162,89,18,67,189,150,188,185,
53,97,44,218,198,120,223,80,183,251,109,91,153,201,107,10,
86,150,121,44,243,56,175,90,100,7,153,37,109,239,13,138,28]
PROGRAM=r'''
#include "pm/v7_secp256k1_signer.hpp"
#include <array>
#include <cstdint>
#include <cstdio>
using namespace pm::v7::clob_signing;
int main(){
 std::array<std::uint8_t,32> key{}; key[31]=1;
 const std::array<std::uint8_t,32> digest{
 42,252,169,70,38,219,145,178,213,86,195,81,88,74,76,217,
 62,19,218,50,227,119,220,72,199,152,62,67,175,165,171,71};
 RecoverableSecp256k1Signer signer(key); if(!signer.valid()) return 2;
 RecoverableSignature65 a{},b{};
 if(!signer.sign_digest(digest,a)||!signer.sign_digest(digest,b)) return 3;
 if(a.bytes!=b.bytes) return 4;
 for(auto v:a.bytes) std::printf("%u,",static_cast<unsigned>(v)); std::printf("\n");
 std::array<char,132> hex{}; if(!signature_hex_0x(a,hex)) return 5;
 std::printf("%.*s\n",132,hex.data());
 std::array<std::uint8_t,32> zero{}; RecoverableSecp256k1Signer invalid(zero);
 if(invalid.valid()) return 6; return 0;
}
'''
def test_recoverable_signer_matches_eth_account_vector():
    compiler=shutil.which('c++'); assert compiler
    assert subprocess.run(['pkg-config','--exists','libsecp256k1']).returncode == 0
    flags=subprocess.check_output(['pkg-config','--cflags','--libs','libsecp256k1'],text=True).strip()
    with tempfile.TemporaryDirectory() as tmp:
        p=Path(tmp); src=p/'main.cpp'; binary=p/'signer-test'; src.write_text(PROGRAM)
        subprocess.run([compiler,'-std=c++20','-O2','-Wall','-Wextra','-Wpedantic',
            f'-I{ROOT / "include"}',str(ROOT/'src/v7_secp256k1_signer.cpp'),str(src),
            *shlex.split(flags),'-o',str(binary)],check=True,capture_output=True,text=True)
        result=subprocess.run([str(binary)],check=True,capture_output=True,text=True)
    lines=result.stdout.splitlines(); got=[int(x) for x in lines[0].split(',') if x]
    assert got==EXPECTED
    assert len(lines[1])==132 and lines[1].startswith('0x') and lines[1].endswith('1c')
