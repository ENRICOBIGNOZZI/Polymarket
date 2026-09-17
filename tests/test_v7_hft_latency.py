from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = r'''
#include "pm/v7_hft_latency.hpp"
#include <cassert>
using namespace pm::v7::latency;
int main(){
 HftLatencyTrace t;
 t.causal_id=7;
 assert(t.record(Stamp::ReadComplete,100));
 assert(t.record(Stamp::ParseComplete,130));
 assert(t.record(Stamp::StateApplied,150));
 assert(t.record(Stamp::SignalReady,170));
 assert(t.record(Stamp::DecisionComplete,210));
 assert(t.record(Stamp::RiskAdmitted,225));
 assert(t.record(Stamp::EncodeComplete,240));
 assert(t.record(Stamp::SignComplete,270));
 assert(t.record(Stamp::WireComplete,300));
 assert(t.record(Stamp::FirstResponseByte,500));
 assert(t.record(Stamp::ResponseComplete,550));
 assert(t.record(Stamp::UserWsReceive,700));
 assert(leg(t,Stamp::ReadComplete,Stamp::ParseComplete).ns==30);
 assert(leg(t,Stamp::WireComplete,Stamp::FirstResponseByte).ns==200);
 assert(leg(t,Stamp::ReadComplete,Stamp::ResponseComplete).ns==450);
 assert(!leg(t,Stamp::KernelRx,Stamp::ReadComplete).valid);
 assert(!t.record_kernel_rx(90,RxTimestampSource::None));
 assert(t.record_kernel_rx(90,RxTimestampSource::HardwareNicTimestamp));
 assert(leg(t,Stamp::KernelRx,Stamp::ReadComplete).ns==10);

 FixedLatencySeries<1000> series;
 for(long long i=1;i<=1000;++i) series.add(i);
 auto d=series.distribution();
 assert(d.samples==1000);
 assert(d.p50_ns==500);
 assert(d.p95_ns==950);
 assert(d.p99_ns==990);
 assert(d.p99_9_ns==999);
 assert(d.max_ns==1000);
 series.add(LatencyLeg{1234,0});
 assert(series.accepted()==1000);
 return 0;
}
'''


def test_hft_stage_trace_and_distribution() -> None:
    cxx = shutil.which("c++")
    assert cxx
    with tempfile.TemporaryDirectory() as td:
        path = Path(td)
        source = path / "main.cpp"
        binary = path / "hft-latency-test"
        source.write_text(PROGRAM)
        subprocess.run(
            [
                cxx,
                "-std=c++20",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Wpedantic",
                f"-I{ROOT / 'include'}",
                str(source),
                "-o",
                str(binary),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run([str(binary)], check=True)
