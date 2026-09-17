from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = r'''
#include "pm/v7_external_first_arrival.hpp"
#include <cassert>
using namespace pm::v7::external_fair;
ExternalVenueEvent ev(VenueId venue,unsigned long long epoch,unsigned long long seq,long long rx){
 ExternalVenueEvent e; e.asset_handle=1; e.venue=venue; e.connection_epoch=epoch;
 e.source_sequence=seq; e.local_receive_monotonic_ns=rx; e.healthy=1; return e;
}
int main(){
 FirstArrivalGate g;
 // Binance is immediately actionable: no Coinbase/Bybit readiness requirement.
 auto b1=g.on_event(ev(VenueId::BinanceSpot,1,1,100));
 assert(b1.evaluate && b1.trigger_sequence==1 && b1.trigger_receive_monotonic_ns==100);
 // A later Coinbase first event is independently actionable.
 auto c1=g.on_event(ev(VenueId::CoinbaseSpot,8,1,105));
 assert(c1.evaluate && c1.trigger_sequence==2);
 // Per-venue duplicate and temporal regression fail closed without poisoning other lanes.
 auto dup=g.on_event(ev(VenueId::BinanceSpot,1,1,110));
 assert(!dup.evaluate && dup.duplicate_or_stale);
 auto reg=g.on_event(ev(VenueId::BinanceSpot,1,2,90));
 assert(!reg.evaluate && reg.temporal_regression);
 auto c2=g.on_event(ev(VenueId::CoinbaseSpot,8,2,120));
 assert(c2.evaluate && c2.trigger_sequence==3);
 // A gapped lane is blocked until its own recovery; other venues still trigger.
 auto gap=ev(VenueId::BybitSpot,3,1,130); gap.gap=1;
 auto gr=g.on_event(gap); assert(!gr.evaluate && gr.unhealthy_or_gap);
 auto c3=g.on_event(ev(VenueId::CoinbaseSpot,8,3,135)); assert(c3.evaluate);
 assert(g.mark_recovered(VenueId::BybitSpot,4));
 auto y1=g.on_event(ev(VenueId::BybitSpot,4,1,140)); assert(y1.evaluate);
 // Binance needs explicit recovery after its receive-time regression.
 auto b2=g.on_event(ev(VenueId::BinanceSpot,1,3,150)); assert(!b2.evaluate && b2.unhealthy_or_gap);
 assert(g.mark_recovered(VenueId::BinanceSpot,2));
 auto b3=g.on_event(ev(VenueId::BinanceSpot,2,1,160)); assert(b3.evaluate);
 return 0;
}
'''


def test_first_arrival_has_no_cross_venue_barrier() -> None:
    cxx = shutil.which("c++")
    assert cxx
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        src = p / "main.cpp"
        exe = p / "first-arrival-test"
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
                str(src),
                "-o",
                str(exe),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run([str(exe)], check=True)
