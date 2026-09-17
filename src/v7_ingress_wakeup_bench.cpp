#include "pm/v7_ingress_wakeup.hpp"

#include <algorithm>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string_view>
#include <thread>
#include <vector>

using pm::v7::external_fair::IngressWakeup;
using namespace std::chrono_literals;

namespace {
std::int64_t now_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

template <class T>
T integer_arg(std::string_view text, T lo, T hi) {
    T value{};
    const auto result = std::from_chars(text.data(), text.data() + text.size(), value);
    if (result.ec != std::errc{} || result.ptr != text.data() + text.size()
        || value < lo || value > hi) throw std::invalid_argument("invalid integer option");
    return value;
}

void cpu_relax() noexcept {
#if defined(__x86_64__) || defined(__i386__)
    __builtin_ia32_pause();
#elif defined(__aarch64__) || defined(__arm__)
    __asm__ __volatile__("yield" ::: "memory");
#else
    std::atomic_signal_fence(std::memory_order_seq_cst);
#endif
}

std::int64_t percentile(std::vector<std::int64_t> values, double p) {
    if (values.empty()) return -1;
    std::sort(values.begin(), values.end());
    const auto index = static_cast<std::size_t>(std::ceil(p * values.size())) - 1;
    return values[std::min(index, values.size() - 1)];
}
} // namespace

int main(int argc, char** argv) {
    try {
        int samples = 20000;
        int spin_us = 50;
        int producer_delay_us = 10;
        for (int i = 1; i < argc; ++i) {
            const std::string_view arg = argv[i];
            auto next = [&]() -> std::string_view {
                if (++i >= argc) throw std::invalid_argument("missing option value");
                return argv[i];
            };
            if (arg == "--samples") samples = integer_arg<int>(next(), 100, 2'000'000);
            else if (arg == "--spin-us") spin_us = integer_arg<int>(next(), 0, 5000);
            else if (arg == "--producer-delay-us") producer_delay_us = integer_arg<int>(next(), 0, 5000);
            else throw std::invalid_argument("unknown option");
        }

        IngressWakeup wakeup;
        std::atomic<int> armed{0};
        std::atomic<int> completed{0};
        std::atomic<std::int64_t> sent_ns{0};
        std::atomic<bool> failure{false};
        std::vector<std::int64_t> latency;
        latency.reserve(static_cast<std::size_t>(samples));

        std::thread producer([&] {
            for (int sequence = 1; sequence <= samples; ++sequence) {
                while (armed.load(std::memory_order_acquire) < sequence) cpu_relax();
                const auto delay_end = std::chrono::steady_clock::now()
                    + std::chrono::microseconds(producer_delay_us);
                while (std::chrono::steady_clock::now() < delay_end) cpu_relax();
                const auto sent = now_ns();
                sent_ns.store(sent, std::memory_order_relaxed);
                wakeup.notify();
                while (completed.load(std::memory_order_acquire) < sequence) cpu_relax();
            }
        });

        for (int sequence = 1; sequence <= samples; ++sequence) {
            armed.store(sequence, std::memory_order_release);
            if (!wakeup.wait_for(100ms, std::chrono::microseconds(spin_us))) {
                failure.store(true, std::memory_order_release);
                break;
            }
            const auto received = now_ns();
            const auto sent = sent_ns.load(std::memory_order_relaxed);
            if (sent <= 0 || received < sent) {
                failure.store(true, std::memory_order_release);
                break;
            }
            latency.push_back(received - sent);
            completed.store(sequence, std::memory_order_release);
        }
        if (failure.load(std::memory_order_acquire)) {
            completed.store(samples, std::memory_order_release);
        }
        producer.join();
        if (failure.load(std::memory_order_acquire) || latency.size() != static_cast<std::size_t>(samples)) {
            std::cerr << "wakeup benchmark failed\n";
            return 2;
        }

        const auto p50 = percentile(latency, .50);
        const auto p95 = percentile(latency, .95);
        const auto p99 = percentile(latency, .99);
        const auto p999 = percentile(latency, .999);
        const auto maximum = *std::max_element(latency.begin(), latency.end());
        std::cout << "{\"schema\":\"polymarket_v7_ingress_wakeup_bench_v1\""
                  << ",\"samples\":" << samples
                  << ",\"spin_us\":" << spin_us
                  << ",\"producer_delay_us\":" << producer_delay_us
                  << ",\"p50_ns\":" << p50
                  << ",\"p95_ns\":" << p95
                  << ",\"p99_ns\":" << p99
                  << ",\"p999_ns\":" << p999
                  << ",\"max_ns\":" << maximum
                  << ",\"kernel_wakeups\":" << wakeup.kernel_wakeups()
                  << ",\"errors\":" << wakeup.errors() << "}\n";
        return wakeup.errors() == 0 ? 0 : 3;
    } catch (const std::exception& error) {
        std::cerr << "wakeup_bench: " << error.what() << '\n';
        return 64;
    }
}
