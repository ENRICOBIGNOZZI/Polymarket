#include "pm/v7_external_tape.hpp"

#include <algorithm>
#include <chrono>
#include <fstream>
#include <stdexcept>
#include <string_view>
#include <thread>

namespace pm::v7::external_fair {
namespace {

template <std::size_t N>
void copy_fixed(std::array<char, N>& target,
                std::string_view value,
                std::string_view field) {
    if (value.empty() || value.size() >= N) {
        throw std::invalid_argument(std::string(field) + " invalid for V7 tape header");
    }
    std::copy(value.begin(), value.end(), target.begin());
    target[value.size()] = '\0';
}

[[nodiscard]] bool exact_sha(std::string_view value) noexcept {
    if (value.size() != 40) return false;
    for (const char ch : value) {
        const bool digit = ch >= '0' && ch <= '9';
        const bool lower_hex = ch >= 'a' && ch <= 'f';
        if (!digit && !lower_hex) return false;
    }
    return true;
}

} // namespace

struct ExternalTapeRecorder::Impl {
    SpscRing<TapeRecord, kExternalTapeQueueCapacity> queue{};
    std::ofstream output;
    std::thread worker;
    std::atomic<bool> stop_requested{false};
    std::atomic<std::uint64_t> accepted{0};
    std::atomic<std::uint64_t> written{0};
    std::atomic<std::uint64_t> dropped{0};
    std::atomic<bool> evidence_valid{true};
    std::atomic<bool> writer_healthy{true};

    Impl(std::filesystem::path path,
         std::string code_sha,
         std::string run_id,
         std::string session_id,
         std::string source,
         std::int64_t creation_wall_ns) {
        if (!exact_sha(code_sha)) {
            throw std::invalid_argument("code_sha must be exact 40-char lowercase hex");
        }
        if (creation_wall_ns <= 0) {
            throw std::invalid_argument("creation_wall_ns must be positive");
        }
        if (!path.parent_path().empty()) {
            std::filesystem::create_directories(path.parent_path());
        }
        output.open(path, std::ios::binary | std::ios::trunc);
        if (!output) throw std::runtime_error("unable to open V7 external tape");

        TapeSessionHeader header;
        header.record_bytes = static_cast<std::uint32_t>(sizeof(TapeRecord));
        header.creation_wall_ns = creation_wall_ns;
        copy_fixed(header.code_sha, code_sha, "code_sha");
        copy_fixed(header.run_id, run_id, "run_id");
        copy_fixed(header.session_id, session_id, "session_id");
        copy_fixed(header.source, source, "source");
        output.write(reinterpret_cast<const char*>(&header), sizeof(header));
        output.flush();
        if (!output) throw std::runtime_error("unable to write V7 tape header");

        worker = std::thread([this] { writer_loop(); });
    }

    ~Impl() {
        stop_requested.store(true, std::memory_order_release);
        if (worker.joinable()) worker.join();
        output.flush();
    }

    void writer_loop() noexcept {
        TapeRecord record;
        while (!stop_requested.load(std::memory_order_acquire)
               || queue.approximate_size() != 0) {
            bool progressed = false;
            while (queue.try_pop(record)) {
                progressed = true;
                output.write(reinterpret_cast<const char*>(&record), sizeof(record));
                if (!output) {
                    writer_healthy.store(false, std::memory_order_release);
                    evidence_valid.store(false, std::memory_order_release);
                    return;
                }
                written.fetch_add(1, std::memory_order_relaxed);
            }
            if (!progressed) std::this_thread::sleep_for(std::chrono::microseconds(100));
        }
        output.flush();
        if (!output) {
            writer_healthy.store(false, std::memory_order_release);
            evidence_valid.store(false, std::memory_order_release);
        }
    }
};

ExternalTapeRecorder::ExternalTapeRecorder(std::filesystem::path path,
                                           std::string code_sha,
                                           std::string run_id,
                                           std::string session_id,
                                           std::string source,
                                           std::int64_t creation_wall_ns)
    : impl_(std::make_unique<Impl>(std::move(path), std::move(code_sha),
                                   std::move(run_id), std::move(session_id),
                                   std::move(source), creation_wall_ns)) {}

ExternalTapeRecorder::~ExternalTapeRecorder() = default;

bool ExternalTapeRecorder::try_record(const TapeRecord& record) noexcept {
    if (!impl_ || record.tape_sequence == 0 || record.receive_monotonic_ns <= 0
        || record.payload_size == 0 || record.payload_size > record.payload.size()) {
        if (impl_) impl_->evidence_valid.store(false, std::memory_order_release);
        return false;
    }
    if (!impl_->writer_healthy.load(std::memory_order_acquire)) {
        impl_->evidence_valid.store(false, std::memory_order_release);
        return false;
    }
    if (!impl_->queue.try_push(record)) {
        impl_->dropped.fetch_add(1, std::memory_order_relaxed);
        impl_->evidence_valid.store(false, std::memory_order_release);
        return false;
    }
    impl_->accepted.fetch_add(1, std::memory_order_relaxed);
    return true;
}

TapeRecorderSnapshot ExternalTapeRecorder::snapshot() const noexcept {
    TapeRecorderSnapshot out;
    if (!impl_) {
        out.evidence_valid = 0;
        out.writer_healthy = 0;
        return out;
    }
    out.accepted = impl_->accepted.load(std::memory_order_acquire);
    out.written = impl_->written.load(std::memory_order_acquire);
    out.dropped = impl_->dropped.load(std::memory_order_acquire);
    out.queued = impl_->queue.approximate_size();
    out.evidence_valid = impl_->evidence_valid.load(std::memory_order_acquire) ? 1 : 0;
    out.writer_healthy = impl_->writer_healthy.load(std::memory_order_acquire) ? 1 : 0;
    return out;
}

} // namespace pm::v7::external_fair
