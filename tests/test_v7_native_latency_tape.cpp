#include "pm/v7_native_latency_tape.hpp"

#include <cassert>
#include <filesystem>
#include <fstream>
#include <thread>
#include <vector>

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

    const auto multi_path = std::filesystem::temp_directory_path()
        / "pm-v7-native-latency-tape-mpsc-test.bin";
    std::filesystem::remove(multi_path, ec);
    NativeLatencyTape multi(multi_path.string());
    constexpr std::uint64_t per_thread = 500;
    std::vector<std::thread> producers;
    for (std::uint64_t producer = 0; producer < 4; ++producer) {
        producers.emplace_back([&, producer] {
            for (std::uint64_t i = 1; i <= per_thread; ++i) {
                NativeLatencyEvent event{};
                event.trace_id = 10'000 + producer * per_thread + i;
                event.client_order_id = event.trace_id;
                event.timestamp_ns = 1'000'000
                    + static_cast<std::int64_t>(producer * per_thread + i);
                event.stage = NativeLatencyStage::HttpAck;
                while (!multi.publish(event)) std::this_thread::yield();
            }
        });
    }
    for (auto& thread : producers) thread.join();
    multi.stop();
    const auto concurrent = multi.snapshot();
    assert(concurrent.published == 4 * per_thread);
    assert(concurrent.written == 4 * per_thread);
    assert(concurrent.dropped == 0);
    assert(std::filesystem::file_size(multi_path)
        == 4 * per_thread * sizeof(NativeLatencyEvent));
    std::filesystem::remove(multi_path, ec);
    return 0;
}
