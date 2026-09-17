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
