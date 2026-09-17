from __future__ import annotations

import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROGRAM = r'''
#include "pm/v7_clob_prepared_body.hpp"
#include "pm/v7_clob_wire.hpp"
#include <array>
#include <charconv>
#include <string>
#include <string_view>
using namespace pm::v7::clob_prepared_body;
using namespace pm::v7::clob_wire;

template <std::size_t N>
std::string_view dec(unsigned long long v, std::array<char,N>& b) {
    auto [e,ec]=std::to_chars(b.data(),b.data()+b.size(),v);
    return ec==std::errc{} ? std::string_view(b.data(),e-b.data()) : std::string_view{};
}

int check(std::string_view side, MarketOrderType type) {
    constexpr std::string_view maker="0x1111111111111111111111111111111111111111";
    constexpr std::string_view zeros="0x0000000000000000000000000000000000000000000000000000000000000000";
    StaticOrderView fixed{zeros,"0",maker,zeros,side,3,maker,"1234",maker,type};
    PreparedPostMarketOrderBody prepared(fixed); if(!prepared.valid()) return 1;
    for (unsigned long long i=1;i<=2000;++i) {
        std::array<char,32> maker_b{}, salt_b{}, taker_b{}, ts_b{};
        const auto maker_amount=dec(10'000'000ULL+i*17ULL,maker_b);
        const auto salt=dec(470'000'000'000ULL+i,salt_b);
        const auto taker_amount=dec(20'000'000ULL+i*19ULL,taker_b);
        const auto timestamp=dec(1'710'000'000'000ULL+i,ts_b);
        const std::string_view signature = (i & 1ULL)
            ? "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            : "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
        SignedMarketOrderView order{zeros,"0",maker,maker_amount,zeros,salt,side,
                                    signature,3,maker,taker_amount,timestamp,"1234"};
        PostMarketOrderView request{order,maker,type};
        std::array<char,2048> canonical{}, fast{};
        const auto n1=serialize_post_market_order(request,canonical);
        const auto n2=prepared.serialize(maker_amount,salt,signature,taker_amount,timestamp,fast);
        if(!n1 || n1!=n2 || std::string_view(canonical.data(),n1)!=std::string_view(fast.data(),n2)) return 2;
    }
    std::array<char,8> small{};
    if(prepared.serialize("1","2","sig","3","4",small)!=0) return 3;
    std::array<char,2048> out{};
    if(prepared.serialize("1","2","bad\"sig","3","4",out)!=0) return 4;
    if(prepared.serialize("1x","2","sig","3","4",out)!=0) return 5;
    return 0;
}

int main(){
    if(check("BUY",MarketOrderType::FAK)!=0) return 10;
    if(check("SELL",MarketOrderType::FOK)!=0) return 11;
    StaticOrderView bad{"x","1","x","x","BUY",0,"x","1","x",MarketOrderType::FAK};
    if(PreparedPostMarketOrderBody(bad).valid()) return 12;
    std::string huge(3000,'x');
    StaticOrderView overflow{huge,"0","0x1111111111111111111111111111111111111111",
        huge,"BUY",0,"0x1111111111111111111111111111111111111111",huge,
        "0x1111111111111111111111111111111111111111",MarketOrderType::FAK};
    if(PreparedPostMarketOrderBody(overflow).valid()) return 13;
    return 0;
}
'''


def test_prepared_body_matches_canonical_byte_for_byte() -> None:
    compiler = shutil.which("c++")
    pkg_config = shutil.which("pkg-config")
    assert compiler and pkg_config
    flags = subprocess.run(
        [pkg_config, "--cflags", "--libs", "openssl"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "main.cpp"
        binary = Path(tmp) / "prepared-body-test"
        source.write_text(PROGRAM)
        subprocess.run([
            compiler, "-std=c++20", "-O2", "-Wall", "-Wextra", "-Wpedantic",
            f"-I{ROOT / 'include'}",
            str(ROOT / "src/v7_clob_prepared_body.cpp"),
            str(ROOT / "src/v7_clob_wire.cpp"),
            str(source), *shlex.split(flags), "-o", str(binary),
        ], check=True, capture_output=True, text=True)
        subprocess.run([str(binary)], check=True, timeout=20)
