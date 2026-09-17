from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = r'''
#include "pm/v7_polymarket_bbo.hpp"
#include <array>
#include <atomic>
#include <cassert>
#include <cstdlib>
#include <new>
#include <vector>
using namespace pm::v7::polymarket_bbo;
static std::atomic<unsigned long long> allocations{0};
void* operator new(std::size_t n){ allocations.fetch_add(1,std::memory_order_relaxed); if(void* p=std::malloc(n)) return p; throw std::bad_alloc(); }
void operator delete(void* p) noexcept { std::free(p); }
void operator delete(void* p,std::size_t) noexcept { std::free(p); }
int main(){
 Decoder d({{"asset-a",11,21,31},{"asset-b",12,22,32}});
 std::array<Update,4> out{};
 constexpr auto price_change=R"({"event_type":"price_change","market":"0xC","timestamp":"123456789000","price_changes":[{"asset_id":"asset-a","price":"0.50","size":"200","side":"BUY","best_bid":".48","best_ask":"0.52"},{"asset_id":"asset-b","price":"0.40","size":"10","side":"SELL","best_bid":"0.3999","best_ask":"0.4001"}]})";
 auto r=d.decode(price_change,1000,out);
 assert(!r.invalid_frame && r.output_count==2 && r.recognized_updates==2);
 assert(out[0].instrument_handle==31 && out[0].best_bid_e4==4800 && out[0].best_ask_e4==5200);
 assert(out[0].exchange_event_ns==123456789000000000LL && out[0].receive_monotonic_ns==1000);
 assert(out[1].instrument_handle==32 && out[1].best_bid_e4==3999 && out[1].best_ask_e4==4001);
 constexpr auto direct=R"({"event_type":"best_bid_ask","asset_id":"asset-a","market":"0xC","timestamp":123456789001,"best_bid":"0.49","best_ask":"0.51","spread":"0.02"})";
 r=d.decode(direct,1100,out);
 assert(r.output_count==1 && out[0].source==SourceKind::BestBidAsk && out[0].best_bid_e4==4900 && out[0].best_ask_e4==5100);
 constexpr auto incomplete=R"({"event_type":"price_change","timestamp":"1","price_changes":[{"asset_id":"asset-a","best_bid":"0.48"}]})";
 r=d.decode(incomplete,1200,out); assert(r.output_count==0 && r.incomplete_bbo==1 && !r.invalid_frame);
 constexpr auto unknown=R"({"event_type":"best_bid_ask","asset_id":"not-bound","timestamp":"1","best_bid":"0.48","best_ask":"0.52"})";
 r=d.decode(unknown,1300,out); assert(r.output_count==0 && r.unknown_assets==1);
 constexpr auto crossed=R"({"event_type":"best_bid_ask","asset_id":"asset-a","timestamp":"1","best_bid":"0.52","best_ask":"0.51"})";
 r=d.decode(crossed,1400,out); assert(r.output_count==0 && r.incomplete_bbo==1);
 // Persistent parser/result arena: ordinary frames allocate no process heap after warmup.
 (void)d.decode(direct,1500,out);
 auto before=allocations.load(std::memory_order_relaxed);
 for(int i=0;i<1000;++i) (void)d.decode(direct,1600+i,out);
 auto after=allocations.load(std::memory_order_relaxed);
 assert(after==before);
 return 0;
}
'''


def test_compact_bbo_decoder() -> None:
    cxx = shutil.which("c++")
    assert cxx
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        src = p / "main.cpp"
        exe = p / "bbo-test"
        src.write_text(PROGRAM)
        subprocess.run(
            [
                cxx,
                "-std=c++20",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Wpedantic",
                f"-I{ROOT / 'include'}",
                "-I/opt/homebrew/include",
                str(ROOT / "src/v7_polymarket_bbo.cpp"),
                str(ROOT / "src/boost_json.cpp"),
                str(src),
                "-o",
                str(exe),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run([str(exe)], check=True)
