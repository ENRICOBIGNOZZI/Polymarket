#include "pm/v7_external_cancel_publisher.hpp"

#include <condition_variable>
#include <fstream>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <unistd.h>

namespace pm::v7::external_fair {
namespace {
namespace fs = std::filesystem;
namespace json = boost::json;

void atomic_write_json(const fs::path& path, const fs::path& temporary,
                       const json::object& value) {
    const std::string serialized = json::serialize(value) + '\n';
    {
        std::ofstream output(temporary, std::ios::binary | std::ios::trunc);
        if (!output) throw std::runtime_error("cannot open external cancel temporary");
        output.write(serialized.data(), static_cast<std::streamsize>(serialized.size()));
        output.flush();
        if (!output) throw std::runtime_error("cannot write external cancel signal");
    }
    fs::rename(temporary, path);
}

} // namespace

struct ExternalCancelSignalPublisher::Impl {
    Impl(fs::path value, JsonBuilder fn)
        : path(std::move(value)), builder(std::move(fn)),
          temporary(path.string() + ".tmp." + std::to_string(::getpid())) {
        if (path.empty() || !builder) throw std::invalid_argument("external cancel publisher requires path and builder");
        if (!path.parent_path().empty()) fs::create_directories(path.parent_path());
        worker = std::thread([this] { run(); });
    }

    ~Impl() { close(); }

    void run() noexcept {
        for (;;) {
            ExternalCancelPublishRecord record;
            if (queue.try_pop(record)) {
                in_flight.store(true, std::memory_order_release);
                try {
                    atomic_write_json(path, temporary, builder(record));
                    written.fetch_add(1, std::memory_order_relaxed);
                } catch (...) {
                    failures.fetch_add(1, std::memory_order_relaxed);
                    healthy_state.store(false, std::memory_order_release);
                    in_flight.store(false, std::memory_order_release);
                    return;
                }
                in_flight.store(false, std::memory_order_release);
                continue;
            }

            std::unique_lock lock(wake_mutex);
            if (stopping && queue.approximate_size() == 0) return;
            wake_cv.wait(lock, [this] {
                return stopping || queue.approximate_size() != 0;
            });
            if (stopping && queue.approximate_size() == 0) return;
        }
    }

    [[nodiscard]] bool publish(const ExternalCancelPublishRecord& record) noexcept {
        if (!healthy_state.load(std::memory_order_acquire)) return false;
        if (!queue.try_push(record)) {
            dropped.fetch_add(1, std::memory_order_relaxed);
            healthy_state.store(false, std::memory_order_release);
            return false;
        }
        submitted.fetch_add(1, std::memory_order_relaxed);
        // Pair with the wait-side mutex so enqueue cannot race into a lost
        // notification between predicate evaluation and sleeping.
        {
            std::lock_guard lock(wake_mutex);
        }
        wake_cv.notify_one();
        return true;
    }

    [[nodiscard]] ExternalCancelPublisherSnapshot snapshot() const noexcept {
        ExternalCancelPublisherSnapshot out;
        out.submitted = submitted.load(std::memory_order_acquire);
        out.written = written.load(std::memory_order_acquire);
        out.dropped = dropped.load(std::memory_order_acquire);
        out.failures = failures.load(std::memory_order_acquire);
        out.queued = queue.approximate_size();
        out.healthy = healthy_state.load(std::memory_order_acquire) ? 1 : 0;
        out.in_flight = in_flight.load(std::memory_order_acquire) ? 1 : 0;
        return out;
    }

    void close() noexcept {
        {
            std::lock_guard lock(wake_mutex);
            stopping = true;
        }
        wake_cv.notify_one();
        if (worker.joinable()) worker.join();
    }

    fs::path path;
    JsonBuilder builder;
    fs::path temporary;
    SpscRing<ExternalCancelPublishRecord, kExternalCancelPublishCapacity> queue{};
    mutable std::mutex wake_mutex{};
    std::condition_variable wake_cv{};
    std::thread worker{};
    bool stopping = false;
    std::atomic<std::uint64_t> submitted{0};
    std::atomic<std::uint64_t> written{0};
    std::atomic<std::uint64_t> dropped{0};
    std::atomic<std::uint64_t> failures{0};
    std::atomic<bool> healthy_state{true};
    std::atomic<bool> in_flight{false};
};

ExternalCancelSignalPublisher::ExternalCancelSignalPublisher(fs::path path, JsonBuilder builder)
    : impl_(std::make_unique<Impl>(std::move(path), std::move(builder))) {}
ExternalCancelSignalPublisher::~ExternalCancelSignalPublisher() = default;
bool ExternalCancelSignalPublisher::publish(const ExternalCancelPublishRecord& record) noexcept {
    return impl_ != nullptr && impl_->publish(record);
}
bool ExternalCancelSignalPublisher::healthy() const noexcept {
    return impl_ != nullptr && impl_->healthy_state.load(std::memory_order_acquire);
}
ExternalCancelPublisherSnapshot ExternalCancelSignalPublisher::snapshot() const noexcept {
    return impl_ != nullptr ? impl_->snapshot() : ExternalCancelPublisherSnapshot{};
}
void ExternalCancelSignalPublisher::close() noexcept { if (impl_ != nullptr) impl_->close(); }

} // namespace pm::v7::external_fair
