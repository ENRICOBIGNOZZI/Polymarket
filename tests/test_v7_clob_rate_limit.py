from __future__ import annotations
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = r'''
#include "pm/v7_clob_rate_limit.hpp"
#include <algorithm>
#include <cassert>
#include <cstdint>
using namespace pm::v7::clob;
struct RefBucket {
 RateWindowConfig c{}; std::uint64_t t=0; std::int64_t last=0;
 void reset(RateWindowConfig x,std::int64_t n){c=x;t=std::uint64_t(c.capacity)<<32U;last=n;}
 bool acq(std::int64_t n,std::uint32_t u=1){if(c.capacity==0||c.window_ns<=0||u==0||u>c.capacity||n<last)return false;if(n>last){auto e=std::uint64_t(n-last),cap=std::uint64_t(c.capacity)<<32U;unsigned __int128 g=(unsigned __int128)e*cap/(unsigned __int128)c.window_ns;t=(std::uint64_t)std::min<unsigned __int128>(cap,(unsigned __int128)t+g);last=n;}auto cost=std::uint64_t(u)<<32U;if(t<cost)return false;t-=cost;return true;}
};
struct RefLane { RefBucket b,s; void reset(LaneRateConfig c,std::int64_t n){b.reset(c.burst,n);s.reset(c.sustained,n);} bool acq(std::int64_t n,std::uint32_t u){auto x=b,y=s;if(!x.acq(n,u)||!y.acq(n,u))return false;b=x;s=y;return true;} };
struct RefLimiter { RefLane o,c; RefLimiter(LaneRateConfig a,LaneRateConfig b){o.reset(a,0);c.reset(b,0);} bool acq(RateLane l,std::int64_t n,std::uint32_t u){return l==RateLane::Cancel?c.acq(n,u):o.acq(n,u);} };
int main(){
 constexpr long long S=1000000000LL;
 LaneRateConfig order{{2,10*S},{4,60*S}};
 LaneRateConfig cancel{{1,10*S},{2,60*S}};
 ClobRateLimiter r(order,cancel);
 assert(r.try_acquire(RateLane::Order,0));
 assert(r.try_acquire(RateLane::Order,0));
 assert(!r.try_acquire(RateLane::Order,0));
 assert(r.try_acquire(RateLane::Cancel,0));
 assert(!r.try_acquire(RateLane::Cancel,0));
 assert(r.try_acquire(RateLane::Cancel,10*S));
 assert(r.try_acquire(RateLane::Order,10*S));
 assert(!r.try_acquire(RateLane::Order,9*S));
 // Freeze decision-level parity against the previous exact Q32 refill formula.
 LaneRateConfig parity{{17,997000003LL},{113,61000000019LL}};
 ClobRateLimiter fast(parity,parity); RefLimiter ref(parity,parity);
 std::uint64_t seed=7; std::int64_t now=0;
 for(int i=0;i<250000;++i){
  seed=seed*6364136223846793005ULL+1; now+=(std::int64_t)((seed>>33)%5000000ULL);
  seed=seed*6364136223846793005ULL+1; auto units=1U+(std::uint32_t)((seed>>40)%3U);
  auto lane=(seed&1U)?RateLane::Order:RateLane::Cancel;
  assert(fast.try_acquire(lane,now,units)==ref.acq(lane,now,units));
 }
 return 0;
}
'''

def test_separate_order_cancel_buckets() -> None:
    cxx = shutil.which("c++")
    assert cxx
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        src = p / "main.cpp"
        exe = p / "test"
        src.write_text(PROGRAM)
        subprocess.run(
            [cxx, "-std=c++20", "-O2", f"-I{ROOT / 'include'}", str(src), "-o", str(exe)],
            check=True,
        )
        subprocess.run([str(exe)], check=True)
