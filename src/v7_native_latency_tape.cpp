#include "pm/v7_native_latency_tape.hpp"

#include <array>
#include <bit>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <thread>

namespace pm::v7 {
namespace {
constexpr std::size_t kLatencyTapeCapacity = 32768;
constexpr std::size_t kLatencyTapeBatch = 256;
static_assert(std::has_single_bit(kLatencyTapeCapacity));

class LatencyMpscRing final {
public:
    LatencyMpscRing() noexcept {
        for (std::size_t i = 0; i < slots_.size(); ++i) {
            slots_[i].sequence.store(i, std::memory_order_relaxed);
        }
    }

    [[nodiscard]] bool try_push(const NativeLatencyEvent& value) noexcept {
        std::size_t pos = head_.load(std::memory_order_relaxed);
        for (;;) {
            auto& slot = slots_[pos & mask_];
            const auto sequence = slot.sequence.load(std::memory_order_acquire);
            const auto diff = static_cast<std::intptr_t>(sequence)
                - static_cast<std::intptr_t>(pos);
            if (diff == 0) {
                if (head_.compare_exchange_weak(
                        pos, pos + 1,
                        std::memory_order_relaxed,
                        std::memory_order_relaxed)) {
                    slot.value = value;
                    slot.sequence.store(pos + 1, std::memory_order_release);
                    return true;
                }
                continue;
            }
            if (diff < 0) return false;
            pos = head_.load(std::memory_order_relaxed);
        }
    }

    [[nodiscard]] bool try_pop(NativeLatencyEvent& value) noexcept {
        const auto pos = tail_.load(std::memory_order_relaxed);
        auto& slot = slots_[pos & mask_];
        const auto sequence = slot.sequence.load(std::memory_order_acquire);
        const auto diff = static_cast<std::intptr_t>(sequence)
            - static_cast<std::intptr_t>(pos + 1);
        if (diff != 0) return false;
        value = slot.value;
        slot.sequence.store(pos + kLatencyTapeCapacity, std::memory_order_release);
        tail_.store(pos + 1, std::memory_order_release);
        return true;
    }

    [[nodiscard]] std::size_t approximate_size() const noexcept {
        const auto head = head_.load(std::memory_order_acquire);
        const auto tail = tail_.load(std::memory_order_acquire);
        return head >= tail ? head - tail : 0;
    }

private:
    struct Slot {
        std::atomic<std::size_t> sequence{0};
        NativeLatencyEvent value{};
    };
    static constexpr std::size_t mask_ = kLatencyTapeCapacity - 1;
    std::array<Slot, kLatencyTapeCapacity> slots_{};
    alignas(64) std::atomic<std::size_t> head_{0};
    alignas(64) std::atomic<std::size_t> tail_{0};
};
}

struct NativeLatencyTape::Impl {
    LatencyMpscRing queue{};
    std::atomic<bool> stopping{false};
    std::atomic<bool> healthy{false};
    std::atomic<std::uint64_t> published{0};
    std::atomic<std::uint64_t> written{0};
    std::atomic<std::uint64_t> dropped{0};
    std::ofstream output;
    std::thread writer;

    explicit Impl(std::string_view raw_path) {
        try {
            const std::filesystem::path path(raw_path);
            if (path.empty()) return;
            if (path.has_parent_path()) {
                std::filesystem::create_directories(path.parent_path());
            }
            output.open(path, std::ios::binary | std::ios::app);
            if (!output) return;
            healthy.store(true, std::memory_order_release);
            writer = std::thread([this] { run(); });
        } catch (...) {
            healthy.store(false, std::memory_order_release);
        }
    }

    void run() noexcept {
        NativeLatencyEvent event{};
        while (!stopping.load(std::memory_order_acquire)
               || queue.approximate_size() != 0) {
            std::size_t count = 0;
            while (count < kLatencyTapeBatch && queue.try_pop(event)) {
                output.write(
                    reinterpret_cast<const char*>(&event),
                    static_cast<std::streamsize>(sizeof(event)));
                if (!output) {
                    healthy.store(false, std::memory_order_release);
                    stopping.store(true, std::memory_order_release);
                    return;
                }
                written.fetch_add(1, std::memory_order_release);
                ++count;
            }
            if (count != 0) {
                output.flush();
                if (!output) {
                    healthy.store(false, std::memory_order_release);
                    stopping.store(true, std::memory_order_release);
                    return;
                }
            } else {
                std::this_thread::sleep_for(std::chrono::milliseconds(1));
            }
        }
        output.flush();
    }

    void stop() noexcept {
        stopping.store(true, std::memory_order_release);
        if (writer.joinable()) writer.join();
    }
};

NativeLatencyTape::NativeLatencyTape(std::string_view path)
    : impl_(std::make_unique<Impl>(path)) {}

NativeLatencyTape::~NativeLatencyTape() {
    stop();
}

bool NativeLatencyTape::publish(const NativeLatencyEvent& event) noexcept {
    if (!impl_ || !impl_->healthy.load(std::memory_order_acquire)
        || impl_->stopping.load(std::memory_order_acquire)
        || event.trace_id == 0 || event.timestamp_ns <= 0) {
        return false;
    }
    if (!impl_->queue.try_push(event)) {
        impl_->dropped.fetch_add(1, std::memory_order_relaxed);
        return false;
    }
    impl_->published.fetch_add(1, std::memory_order_release);
    return true;
}

void NativeLatencyTape::stop() noexcept {
    if (impl_) impl_->stop();
}

NativeLatencyTapeSnapshot NativeLatencyTape::snapshot() const noexcept {
    NativeLatencyTapeSnapshot out{};
    if (!impl_) return out;
    out.published = impl_->published.load(std::memory_order_acquire);
    out.written = impl_->written.load(std::memory_order_acquire);
    out.dropped = impl_->dropped.load(std::memory_order_acquire);
    out.queued = impl_->queue.approximate_size();
    out.healthy = impl_->healthy.load(std::memory_order_acquire) ? 1 : 0;
    return out;
}

} // namespace pm::v7
