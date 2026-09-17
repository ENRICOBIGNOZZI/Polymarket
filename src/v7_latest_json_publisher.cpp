#include "pm/v7_latest_json_publisher.hpp"

#include <atomic>
#include <condition_variable>
#include <fstream>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <thread>
#include <unistd.h>

namespace pm::v7::external_fair {
namespace {
namespace fs = std::filesystem;
namespace json = boost::json;

void atomic_write_json(const fs::path& path, const json::object& value) {
    const fs::path temporary = path.string() + ".tmp." + std::to_string(::getpid());
    const std::string serialized = json::serialize(value) + '\n';
    {
        std::ofstream output(temporary, std::ios::binary | std::ios::trunc);
        if (!output) throw std::runtime_error("cannot open status temporary");
        output.write(serialized.data(), static_cast<std::streamsize>(serialized.size()));
        output.flush();
        if (!output) throw std::runtime_error("cannot write status temporary");
    }
    fs::rename(temporary, path);
}

} // namespace

struct LatestJsonPublisher::Impl {
    explicit Impl(fs::path value) : path(std::move(value)) {
        if (path.empty()) throw std::invalid_argument("status path must not be empty");
        if (!path.parent_path().empty()) fs::create_directories(path.parent_path());
        worker = std::thread([this] { run(); });
    }

    ~Impl() { close(); }

    void run() noexcept {
        for (;;) {
            std::optional<json::object> next;
            {
                std::unique_lock lock(mutex);
                cv.wait(lock, [&] { return stopping || pending.has_value(); });
                if (!pending.has_value()) {
                    if (stopping) return;
                    continue;
                }
                next.emplace(std::move(*pending));
                pending.reset();
                in_flight.store(true, std::memory_order_release);
            }
            try {
                atomic_write_json(path, *next);
                written.fetch_add(1, std::memory_order_relaxed);
                in_flight.store(false, std::memory_order_release);
            } catch (...) {
                failures.fetch_add(1, std::memory_order_relaxed);
                healthy.store(false, std::memory_order_release);
                in_flight.store(false, std::memory_order_release);
                std::lock_guard lock(mutex);
                pending.reset();
                stopping = true;
                cv.notify_all();
                return;
            }
        }
    }

    [[nodiscard]] bool publish(json::object value) noexcept {
        if (!healthy.load(std::memory_order_acquire)) return false;
        try {
            {
                std::lock_guard lock(mutex);
                if (stopping || !healthy.load(std::memory_order_relaxed)) return false;
                if (pending.has_value()) coalesced.fetch_add(1, std::memory_order_relaxed);
                pending.emplace(std::move(value));
                submitted.fetch_add(1, std::memory_order_relaxed);
            }
            cv.notify_one();
            return true;
        } catch (...) {
            failures.fetch_add(1, std::memory_order_relaxed);
            healthy.store(false, std::memory_order_release);
            return false;
        }
    }

    void close() noexcept {
        {
            std::lock_guard lock(mutex);
            stopping = true;
        }
        cv.notify_all();
        if (worker.joinable()) worker.join();
    }

    [[nodiscard]] LatestJsonPublisherSnapshot snapshot() const noexcept {
        LatestJsonPublisherSnapshot out;
        out.submitted = submitted.load(std::memory_order_acquire);
        out.written = written.load(std::memory_order_acquire);
        out.coalesced = coalesced.load(std::memory_order_acquire);
        out.failures = failures.load(std::memory_order_acquire);
        out.healthy = healthy.load(std::memory_order_acquire) ? 1 : 0;
        out.in_flight = in_flight.load(std::memory_order_acquire) ? 1 : 0;
        {
            std::lock_guard lock(mutex);
            out.pending = pending.has_value() ? 1 : 0;
        }
        return out;
    }

    fs::path path;
    mutable std::mutex mutex{};
    std::condition_variable cv{};
    std::optional<json::object> pending{};
    std::thread worker{};
    bool stopping = false;
    std::atomic<std::uint64_t> submitted{0};
    std::atomic<std::uint64_t> written{0};
    std::atomic<std::uint64_t> coalesced{0};
    std::atomic<std::uint64_t> failures{0};
    std::atomic<bool> healthy{true};
    std::atomic<bool> in_flight{false};
};

LatestJsonPublisher::LatestJsonPublisher(fs::path path)
    : impl_(std::make_unique<Impl>(std::move(path))) {}

LatestJsonPublisher::~LatestJsonPublisher() = default;

bool LatestJsonPublisher::publish(json::object value) noexcept {
    return impl_ != nullptr && impl_->publish(std::move(value));
}

LatestJsonPublisherSnapshot LatestJsonPublisher::snapshot() const noexcept {
    return impl_ != nullptr ? impl_->snapshot() : LatestJsonPublisherSnapshot{};
}

void LatestJsonPublisher::close() noexcept {
    if (impl_ != nullptr) impl_->close();
}

} // namespace pm::v7::external_fair
