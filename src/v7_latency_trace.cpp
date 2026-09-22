#include "pm/v7_latency_trace.hpp"
#include "pm/v7_spsc.hpp"

#include <array>
#include <atomic>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <thread>

namespace pm::v7 {
namespace fs = std::filesystem;
using namespace std::chrono_literals;

struct NativeLatencyTraceWriter::Impl {
    static constexpr std::size_t kQueueCapacity = 16'384;
    static constexpr std::size_t kBatch = 256;

    SpscRing<LatencyTraceRecord, kQueueCapacity> queue{};
    std::ofstream output{};
    std::thread writer{};
    std::atomic<bool> stopping{false};
    std::atomic<bool> healthy{false};
    std::atomic<std::uint64_t> published{0};
    std::atomic<std::uint64_t> written{0};
    std::atomic<std::uint64_t> dropped{0};

    explicit Impl(std::string_view raw_path) {
        try {
            const fs::path path(raw_path);
            if (path.empty()) return;
            if (!path.parent_path().empty()) fs::create_directories(path.parent_path());
            const bool exists = fs::exists(path) && fs::file_size(path) > 0;
            output.open(path, std::ios::binary | std::ios::app);
            if (!output) return;
            if (!exists) {
                LatencyTraceFileHeader header{};
                header.record_size = static_cast<std::uint32_t>(sizeof(LatencyTraceRecord));
                output.write(reinterpret_cast<const char*>(&header), sizeof(header));
                output.flush();
                if (!output) return;
            }
            healthy.store(true, std::memory_order_release);
            writer = std::thread([this] { run(); });
        } catch (...) {
            healthy.store(false, std::memory_order_release);
        }
    }

    ~Impl() { stop(); }

    void run() noexcept {
        std::array<LatencyTraceRecord, kBatch> batch{};
        while (!stopping.load(std::memory_order_acquire)
               || queue.approximate_size() != 0) {
            std::size_t count = 0;
            while (count < batch.size() && queue.try_pop(batch[count])) ++count;
            if (count == 0) {
                std::this_thread::sleep_for(1ms);
                continue;
            }
            output.write(reinterpret_cast<const char*>(batch.data()),
                         static_cast<std::streamsize>(
                             count * sizeof(LatencyTraceRecord)));
            if (!output) {
                healthy.store(false, std::memory_order_release);
                return;
            }
            written.fetch_add(count, std::memory_order_relaxed);
        }
        output.flush();
        if (!output) healthy.store(false, std::memory_order_release);
    }

    void stop() noexcept {
        stopping.store(true, std::memory_order_release);
        if (writer.joinable()) writer.join();
        if (output.is_open()) output.flush();
    }
};

NativeLatencyTraceWriter::NativeLatencyTraceWriter(std::string_view path)
    : impl_(std::make_unique<Impl>(path)) {}

NativeLatencyTraceWriter::~NativeLatencyTraceWriter() = default;

bool NativeLatencyTraceWriter::valid() const noexcept {
    return impl_ != nullptr && impl_->healthy.load(std::memory_order_acquire);
}

bool NativeLatencyTraceWriter::publish(const LatencyTraceRecord& record) noexcept {
    if (!valid() || record.trace_id == 0 || record.valid_mask == 0) return false;
    if (!impl_->queue.try_push(record)) {
        impl_->dropped.fetch_add(1, std::memory_order_relaxed);
        return false;
    }
    impl_->published.fetch_add(1, std::memory_order_relaxed);
    return true;
}

void NativeLatencyTraceWriter::stop() noexcept {
    if (impl_) impl_->stop();
}

LatencyTraceWriterSnapshot NativeLatencyTraceWriter::snapshot() const noexcept {
    LatencyTraceWriterSnapshot out{};
    if (!impl_) return out;
    out.published = impl_->published.load(std::memory_order_relaxed);
    out.written = impl_->written.load(std::memory_order_relaxed);
    out.dropped = impl_->dropped.load(std::memory_order_relaxed);
    out.queued = impl_->queue.approximate_size();
    out.healthy = impl_->healthy.load(std::memory_order_acquire) ? 1 : 0;
    return out;
}

} // namespace pm::v7
