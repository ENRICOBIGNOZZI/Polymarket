#include "pm/v7_native_latency_tape.hpp"

#include <cassert>
#include <filesystem>
#include <fstream>

using namespace pm::v7;

int main() {
    const auto path = std::filesystem::temp_directory_path()
        / "pm-v7-native-latency-tape-test.bin";
    std::error_code ec;
    std::filesystem::remove(path, ec);

    NativeLatencyTape tape(path.string());
    for (std::uint64_t i = 1; i <= 100; ++i) {
        NativeLatencyEvent event{};
        event.trace_id = i;
        event.client_order_id = 1000 + i;
        event.market_handle = 7;
        event.instrument_handle = 11;
        event.timestamp_ns = static_cast<std::int64_t>(10'000 + i);
        event.stage = NativeLatencyStage::ArbDecision;
        assert(tape.publish(event));
    }
    tape.stop();
    const auto snapshot = tape.snapshot();
    assert(snapshot.healthy == 1);
    assert(snapshot.published == 100);
    assert(snapshot.written == 100);
    assert(snapshot.dropped == 0);
    assert(snapshot.queued == 0);
    assert(std::filesystem::file_size(path) == 100 * sizeof(NativeLatencyEvent));

    NativeLatencyEvent rejected{};
    rejected.trace_id = 999;
    rejected.timestamp_ns = 1;
    assert(!tape.publish(rejected));

    std::filesystem::remove(path, ec);
    return 0;
}
