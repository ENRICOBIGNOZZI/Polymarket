#include "pm/v7_external_cancel_publisher.hpp"

#include <boost/json.hpp>

#include <cassert>
#include <atomic>
#include <thread>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <vector>
#include <string>
#include <unistd.h>

using namespace pm::v7::external_fair;
namespace fs = std::filesystem;
namespace json = boost::json;

int main() {
    const fs::path root = fs::temp_directory_path()
        / ("pm_v7_external_cancel_publisher_" + std::to_string(::getpid()));
    fs::create_directories(root);
    const fs::path path = root / "signal.json";

    std::vector<std::uint64_t> built_versions;
    built_versions.reserve(100);
    ExternalCancelSignalPublisher publisher(path,
        [&](const ExternalCancelPublishRecord& record) {
            built_versions.push_back(record.signal.signal_version);
            return json::object{
                {"signal_version", record.signal.signal_version},
                {"valid", record.signal.valid != 0},
                {"publish_monotonic_ns", record.publish_monotonic_ns},
                {"publish_wall_ns", record.publish_wall_ns},
            };
        });

    for (std::uint64_t version = 1; version <= 100; ++version) {
        ExternalCancelPublishRecord record;
        record.signal.signal_version = version;
        record.signal.valid = static_cast<std::uint8_t>(version & 1U);
        record.publish_monotonic_ns = 1'000'000 + static_cast<std::int64_t>(version);
        record.publish_wall_ns = 2'000'000 + static_cast<std::int64_t>(version);
        assert(publisher.publish(record));
    }
    publisher.close();
    const auto state = publisher.snapshot();
    assert(state.submitted == 100);
    assert(state.written == 100);
    assert(state.dropped == 0);
    assert(state.failures == 0);
    assert(state.queued == 0);
    assert(state.healthy == 1);
    assert(state.in_flight == 0);
    assert(built_versions.size() == 100);
    for (std::uint64_t version = 1; version <= 100; ++version) {
        assert(built_versions[version - 1] == version);
    }

    std::ifstream input(path, std::ios::binary);
    assert(input);
    const std::string text((std::istreambuf_iterator<char>(input)),
                           std::istreambuf_iterator<char>());
    boost::system::error_code error;
    const auto value = json::parse(text, error);
    assert(!error && value.is_object());
    const auto& object = value.as_object();
    assert(object.at("signal_version").to_number<std::uint64_t>() == 100);
    assert(object.at("valid").as_bool() == false);
    assert(object.at("publish_monotonic_ns").as_int64() == 1'000'100);
    assert(object.at("publish_wall_ns").as_int64() == 2'000'100);

    fs::remove_all(root);

    // Backpressure must fail closed, never coalesce or silently drop. Hold the
    // writer inside its first JSON build, fill the bounded queue, then require
    // an explicit producer failure and unhealthy state.
    const fs::path blocked_root = fs::temp_directory_path()
        / ("pm_v7_external_cancel_blocked_" + std::to_string(::getpid()));
    fs::create_directories(blocked_root);
    std::atomic<bool> release_writer{false};
    std::atomic<bool> writer_entered{false};
    ExternalCancelSignalPublisher blocked(blocked_root / "signal.json",
        [&](const ExternalCancelPublishRecord& record) {
            writer_entered.store(true, std::memory_order_release);
            while (!release_writer.load(std::memory_order_acquire)) {
                std::this_thread::yield();
            }
            return json::object{{"signal_version", record.signal.signal_version}};
        });
    ExternalCancelPublishRecord first;
    first.signal.signal_version = 1;
    assert(blocked.publish(first));
    while (!writer_entered.load(std::memory_order_acquire)) std::this_thread::yield();
    bool rejected = false;
    for (std::uint64_t version = 2; version < 2 + kExternalCancelPublishCapacity + 2; ++version) {
        ExternalCancelPublishRecord record;
        record.signal.signal_version = version;
        if (!blocked.publish(record)) { rejected = true; break; }
    }
    assert(rejected);
    assert(!blocked.healthy());
    auto blocked_state = blocked.snapshot();
    assert(blocked_state.dropped >= 1);
    release_writer.store(true, std::memory_order_release);
    blocked.close();
    fs::remove_all(blocked_root);
    return 0;
}
