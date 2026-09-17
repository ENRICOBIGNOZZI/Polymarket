from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROGRAM = r"""
#include "pm/v7_event_leadlag.hpp"
#include <cassert>
#include <cmath>

using namespace pm::v7::leadlag;
constexpr long long MS = 1'000'000LL;

void seed(EventLeadLagEngine& e) {
    assert(e.on_coinbase_mid(1*MS, 100.0));
    auto s=e.on_binance_trade(1*MS,100.0); assert(s.signal_version==0);
    assert(e.on_coinbase_mid(250*MS,100.0));
    s=e.on_binance_trade(250*MS,100.0); assert(s.signal_version==0);
    assert(e.on_coinbase_mid(360*MS,100.001));
}

int main(){
    EventLeadLagEngine e; seed(e);
    auto s=e.on_binance_trade(362*MS,100.004);
    assert(s.valid && s.signal_version==1 && s.direction==1);
    assert(s.causal_trigger_receive_monotonic_ns==362*MS);
    assert(s.evaluated_receive_monotonic_ns==362*MS);
    assert(s.binance_prior_receive_monotonic_ns==250*MS);
    assert(s.coinbase_prior_receive_monotonic_ns==250*MS);
    assert(s.coinbase_current_receive_monotonic_ns==360*MS);
    assert(s.binance_return_100ms_bp > 0.39 && s.binance_return_100ms_bp < 0.41);
    // V1 grid starting at 1ms would not inspect the 362ms trade until 376ms.
    constexpr long long next_v1_grid = 376*MS;
    static_assert(next_v1_grid - 362*MS == 14*MS);

    // Cooldown suppresses a second otherwise valid shock.
    assert(e.on_coinbase_mid(400*MS,100.002));
    auto c=e.on_binance_trade(401*MS,100.005); assert(c.signal_version==1);
    assert(e.metrics().cooldown_rejects>=1);

    // Opposing Coinbase confirmation suppresses a trigger in a fresh engine.
    EventLeadLagEngine opposite;
    assert(opposite.on_coinbase_mid(1*MS,100.0));
    (void)opposite.on_binance_trade(1*MS,100.0);
    assert(opposite.on_coinbase_mid(250*MS,100.0));
    (void)opposite.on_binance_trade(250*MS,100.0);
    assert(opposite.on_coinbase_mid(360*MS,99.999));
    auto o=opposite.on_binance_trade(362*MS,100.004);
    assert(o.signal_version==0 && opposite.metrics().opposing_confirmation_rejects==1);

    // Global time regression fails closed and destroys the old trigger state.
    assert(!opposite.on_coinbase_mid(300*MS,100.0));
    assert(opposite.snapshot(362*MS).signal_version==0);
    assert(opposite.metrics().out_of_order_events==1);
    // The high-watermark stays at 362ms; a 350ms event cannot sneak in after recovery.
    assert(!opposite.on_coinbase_mid(350*MS,100.0));
    assert(opposite.metrics().out_of_order_events==2);

    // Automatic recovery never reuses an old signal version.
    EventLeadLagEngine versions; seed(versions);
    auto v1=versions.on_binance_trade(362*MS,100.004); assert(v1.signal_version==1);
    assert(!versions.on_coinbase_mid(300*MS,100.0));
    // Re-warm strictly after the retained 362ms watermark.
    assert(versions.on_coinbase_mid(363*MS,100.0));
    (void)versions.on_binance_trade(363*MS,100.0);
    assert(versions.on_coinbase_mid(650*MS,100.0));
    (void)versions.on_binance_trade(650*MS,100.0);
    assert(versions.on_coinbase_mid(670*MS,100.001));
    auto v2=versions.on_binance_trade(672*MS,100.004);
    assert(v2.signal_version==2);

    // More than 8,192 samples inside one 100ms window cannot silently overwrite
    // exact history: the bounded engine fails closed and records the overflow.
    EventLeadLagEngine saturated;
    assert(saturated.on_coinbase_mid(1,100.0));
    for (int i=0;i<8193;++i) (void)saturated.on_binance_trade(2+i,100.0);
    assert(saturated.metrics().history_overflows==1);
    assert(saturated.snapshot(9000).signal_version==0);
    return 0;
}
"""


def test_event_driven_leadlag_v2() -> None:
    compiler = shutil.which("c++")
    assert compiler
    with tempfile.TemporaryDirectory() as tmp:
        p=Path(tmp); main=p/"main.cpp"; binary=p/"test"
        main.write_text(PROGRAM)
        subprocess.run([compiler,"-std=c++20","-O2","-Wall","-Wextra","-Wpedantic",
                        f"-I{ROOT/'include'}",str(ROOT/'src/v7_event_leadlag.cpp'),
                        str(main),"-o",str(binary)],check=True,capture_output=True,text=True)
        subprocess.run([str(binary)],check=True,timeout=10)
