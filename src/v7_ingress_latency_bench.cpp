#include "pm/v7_ingress_wakeup.hpp"
#include "pm/v7_external_ingress.hpp"
#include <boost/json.hpp>

#include <algorithm>
#include <array>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cmath>
#include <ctime>
#include <iostream>
#include <memory>
#include <random>
#include <stdexcept>
#include <string_view>
#include <thread>
#include <vector>

using namespace pm::v7::external_fair;
using namespace std::chrono_literals;
namespace json = boost::json;

namespace {
std::int64_t now_ns() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}
json::object distribution(std::vector<std::int64_t> values) {
    if (values.empty()) return {{"count", 0}, {"p50", nullptr}, {"p95", nullptr}, {"p99", nullptr}, {"max", nullptr}};
    std::sort(values.begin(), values.end());
    const auto q = [&](double p) {
        const auto index = static_cast<std::size_t>(std::ceil(p * static_cast<double>(values.size()))) - 1;
        return static_cast<double>(values[index]) / 1000.0;
    };
    return {{"count", values.size()}, {"p50", q(.50)}, {"p95", q(.95)}, {"p99", q(.99)}, {"max", q(1.0)}};
}
json::object run(bool event_driven, int samples, int round) {
    IngressWakeup wakeup;
    auto ingress = std::make_unique<ExternalVenueIngress>(
        VenueId::BinanceSpot, 500, nullptr, event_driven ? &wakeup : nullptr);
    std::atomic<bool> done{false};
    const auto started = now_ns();
    const auto cpu_start = std::clock();
    std::thread producer([&] {
        std::mt19937 generator(1709U + static_cast<unsigned>(round));
        std::uniform_int_distribution<int> delay(200, 2000);
        for (int i = 0; i < samples; ++i) {
            std::this_thread::sleep_for(std::chrono::microseconds(delay(generator)));
            ExternalVenueEvent event;
            event.venue = VenueId::BinanceSpot;
            event.asset_handle = 500;
            event.connection_epoch = 1;
            event.source_sequence = static_cast<std::uint64_t>(i + 1);
            event.event_type = ExternalEventType::BookTop;
            event.bid = 65000.0; event.ask = 65001.0;
            event.bid_size = 1; event.ask_size = 1; event.healthy = 1;
            event.local_receive_wall_ns = 1; // synthetic; never exchange/network timing
            event.local_receive_monotonic_ns = now_ns();
            (void)ingress->on_event(event);
        }
        done.store(true, std::memory_order_release);
        if (event_driven) wakeup.notify();
    });
    std::array<ExternalVenueEvent, 256> batch;
    std::vector<std::int64_t> latency;
    latency.reserve(static_cast<std::size_t>(samples));
    std::uint64_t expected_sequence = 1;
    bool sequence_ok = true;
    while (!done.load(std::memory_order_acquire) || ingress->snapshot().queued != 0) {
        const auto count = ingress->drain_events(batch);
        const auto received = now_ns();
        for (std::size_t i = 0; i < count; ++i) {
            sequence_ok = sequence_ok && batch[i].source_sequence == expected_sequence++;
            latency.push_back(received - batch[i].local_receive_monotonic_ns);
        }
        if (event_driven) (void)wakeup.wait_for(5ms);
        else std::this_thread::sleep_for(5ms);
    }
    producer.join();
    const auto snapshot = ingress->snapshot();
    const bool valid = sequence_ok && latency.size() == static_cast<std::size_t>(samples)
        && snapshot.dropped_events == 0 && wakeup.errors() == 0;
    return {
        {"mode", event_driven ? "EVENT_DRIVEN" : "POLL_5MS"}, {"round", round},
        {"requested", samples}, {"delivered", latency.size()},
        {"dropped", snapshot.dropped_events}, {"sequence_ok", sequence_ok},
        {"wakeup_errors", wakeup.errors()}, {"valid", valid},
        {"wall_seconds", static_cast<double>(now_ns() - started) / 1e9},
        {"cpu_seconds", static_cast<double>(std::clock() - cpu_start) / CLOCKS_PER_SEC},
        {"queue_handoff_us", distribution(std::move(latency))},
    };
}
int number(std::string_view value, int minimum, int maximum) {
    int out = 0;
    const auto result = std::from_chars(value.data(), value.data() + value.size(), out);
    if (result.ec != std::errc{} || result.ptr != value.data() + value.size() || out < minimum || out > maximum)
        throw std::invalid_argument("benchmark argument outside bounded range");
    return out;
}
}
int main(int argc, char** argv) {
    try {
        int samples = 2000, rounds = 3;
        for (int i = 1; i < argc; ++i) {
            const std::string_view argument = argv[i];
            if (argument == "--samples" && i + 1 < argc) samples = number(argv[++i], 16, 50000);
            else if (argument == "--rounds" && i + 1 < argc) rounds = number(argv[++i], 1, 10);
            else throw std::invalid_argument("unknown or incomplete argument");
        }
        json::array measurements;
        bool valid = true;
        for (int r = 0; r < rounds; ++r) {
            // Alternate order to reduce a fixed warmup/order advantage.
            for (int phase = 0; phase < 2; ++phase) {
                auto measurement = run((r + phase) % 2 != 0, samples, r);
                valid = valid && measurement.at("valid").as_bool();
                measurements.emplace_back(std::move(measurement));
            }
        }
        std::cout << json::serialize(json::object{
            {"schema", "polymarket_v7_ingress_latency_bench_v1"},
            {"scope", "SYNTHETIC_QUEUE_HANDOFF_ONLY_NOT_NETWORK_OR_EXECUTION"},
            {"paper_only", true}, {"authenticated_execution", false},
            {"real_order_submission", false}, {"valid", valid},
            {"measurements", std::move(measurements)}}) << '\n';
        return valid ? 0 : 2;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 64;
    }
}
