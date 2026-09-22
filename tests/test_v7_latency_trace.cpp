#include "pm/v7_latency_trace.hpp"

#include <cassert>
#include <filesystem>

using namespace pm::v7;
namespace fs=std::filesystem;

int main() {
    const auto path=fs::temp_directory_path()/"pm-v7-latency-trace-test.bin";
    fs::remove(path);
    {
        NativeLatencyTraceWriter writer(path.string());
        assert(writer.valid());
        for(std::uint64_t i=1;i<=100;++i) {
            LatencyTraceRecord r{};
            r.trace_id=i;
            r.market_handle=7;
            r.instrument_handle=11;
            r.valid_mask=TraceFrameReceive|TraceDecodeComplete|TraceArbDecision;
            r.frame_receive_monotonic_ns=1'000+static_cast<std::int64_t>(i);
            r.decode_complete_monotonic_ns=r.frame_receive_monotonic_ns+100;
            r.arb_decision_monotonic_ns=r.decode_complete_monotonic_ns+200;
            assert(writer.publish(r));
        }
        writer.stop();
        const auto snap=writer.snapshot();
        assert(snap.healthy==1);
        assert(snap.published==100);
        assert(snap.written==100);
        assert(snap.dropped==0);
        assert(snap.queued==0);
    }
    assert(fs::file_size(path)==sizeof(LatencyTraceFileHeader)
        +100*sizeof(LatencyTraceRecord));
    fs::remove(path);
    return 0;
}
