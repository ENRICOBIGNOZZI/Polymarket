from __future__ import annotations
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = r'''
#include "pm/v7_clob_rate_limit.hpp"
#include <cassert>
using namespace pm::v7::clob;
int main(){
 constexpr long long T0=1;
 constexpr long long STEP=25'000'000LL;
 ClobRateLimiter r;
 // Standard tier starts with independent 60-order and 120-cancel bursts.
 for(int i=0;i<60;++i) assert(r.try_acquire(RateLane::Order,T0,1));
 assert(!r.try_acquire(RateLane::Order,T0,1));
 assert(r.try_acquire(RateLane::Cancel,T0,1));
 auto snap=r.snapshot(T0);
 assert(snap.order_tokens==0.0);
 assert(snap.cancel_tokens==119.0);
 // One Standard order token refills in 25ms; cancel bucket remains separate.
 assert(r.try_acquire(RateLane::Order,T0+STEP,1));
 snap=r.snapshot(T0+STEP);
 assert(snap.order_tokens_consumed==61);
 assert(snap.cancel_tokens_consumed==1);
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
