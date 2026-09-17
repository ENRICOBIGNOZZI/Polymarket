#include "pm/v7_prepared_order_frame.hpp"

#include <algorithm>
#include <cstring>

namespace pm::v7::prepared_order {
namespace {
inline void single_writer_inc(std::atomic<std::uint64_t>& value) noexcept {
    value.store(value.load(std::memory_order_relaxed) + 1, std::memory_order_relaxed);
}
}

bool Cache::key_equal(const Key& a, const Key& b) noexcept {
    return a.instrument_handle == b.instrument_handle
        && a.price_e4 == b.price_e4
        && a.tick_size_e4 == b.tick_size_e4
        && a.quantity_microunits == b.quantity_microunits
        && a.side == b.side
        && a.time_in_force == b.time_in_force;
}

std::uint64_t Cache::pack(std::uint64_t generation, std::uint8_t slot) noexcept {
    return (generation << 1U) | static_cast<std::uint64_t>(slot & 1U);
}
std::uint8_t Cache::unpack_slot(std::uint64_t value) noexcept {
    return static_cast<std::uint8_t>(value & 1U);
}
std::uint64_t Cache::unpack_generation(std::uint64_t value) noexcept {
    return value >> 1U;
}

bool Cache::publish(const Publication& publication,
                    std::span<const char> frame) noexcept {
    if (publication.key.instrument_handle == 0 || publication.key.price_e4 <= 0
        || publication.key.tick_size_e4 <= 0 || publication.key.quantity_microunits <= 0
        || publication.prepared_monotonic_ns <= 0 || publication.order_timestamp_ms == 0
        || publication.salt == 0 || frame.empty() || frame.size() > kMaxFrameBytes) {
        single_writer_inc(publish_blocked_);
        return false;
    }

    const auto current = published_.load(std::memory_order_acquire);
    const std::uint8_t target = current == 0 ? 0 : static_cast<std::uint8_t>(unpack_slot(current) ^ 1U);
    auto& slot = slots_[target];
    if (slot.readers.load(std::memory_order_acquire) != 0) {
        single_writer_inc(publish_blocked_);
        return false;
    }

    std::memcpy(slot.frame.data(), frame.data(), frame.size());
    slot.publication = publication;
    slot.size = frame.size();
    auto generation = ++producer_generation_;
    if (generation == 0) generation = ++producer_generation_;
    slot.generation = generation;
    slot.consumed.store(0, std::memory_order_release);
    published_.store(pack(generation, target), std::memory_order_release);
    single_writer_inc(publishes_);
    return true;
}

AcquireResult Cache::try_acquire(const Requirement& requirement) noexcept {
    AcquireResult out;
    for (int attempt = 0; attempt < 2; ++attempt) {
        const auto published = published_.load(std::memory_order_acquire);
        if (published == 0) {
            single_writer_inc(empty_);
            out.reason = AcquireReason::Empty;
            return out;
        }
        const auto slot_index = unpack_slot(published);
        const auto generation = unpack_generation(published);
        auto& slot = slots_[slot_index];
        slot.readers.fetch_add(1, std::memory_order_acq_rel);

        if (published_.load(std::memory_order_acquire) != published
            || slot.generation != generation) {
            slot.readers.fetch_sub(1, std::memory_order_release);
            single_writer_inc(changed_);
            out.reason = AcquireReason::ChangedDuringAcquire;
            continue;
        }

        if (!key_equal(slot.publication.key, requirement.key)
            || (requirement.require_exact_source_event != 0
                && slot.publication.source_exchange_event_ns != requirement.source_exchange_event_ns)) {
            slot.readers.fetch_sub(1, std::memory_order_release);
            single_writer_inc(mismatch_);
            out.reason = AcquireReason::Mismatch;
            return out;
        }
        if (requirement.now_monotonic_ns <= 0 || requirement.maximum_age_ns < 0
            || slot.publication.prepared_monotonic_ns > requirement.now_monotonic_ns
            || requirement.now_monotonic_ns - slot.publication.prepared_monotonic_ns
                > requirement.maximum_age_ns) {
            slot.readers.fetch_sub(1, std::memory_order_release);
            single_writer_inc(stale_);
            out.reason = AcquireReason::Stale;
            return out;
        }
        std::uint8_t expected = 0;
        if (!slot.consumed.compare_exchange_strong(
                expected, 1, std::memory_order_acq_rel, std::memory_order_acquire)) {
            slot.readers.fetch_sub(1, std::memory_order_release);
            single_writer_inc(consumed_);
            out.reason = AcquireReason::AlreadyConsumed;
            return out;
        }

        out.lease.data = slot.frame.data();
        out.lease.size = slot.size;
        out.lease.publication = slot.publication;
        out.lease.generation = slot.generation;
        out.lease.slot = slot_index;
        out.lease.valid = 1;
        out.reason = AcquireReason::Hit;
        single_writer_inc(hits_);
        return out;
    }
    return out;
}

void Cache::release(Lease& lease) noexcept {
    if (lease.valid == 0 || lease.slot >= slots_.size()) return;
    auto& slot = slots_[lease.slot];
    slot.readers.fetch_sub(1, std::memory_order_release);
    lease = {};
}

void Cache::clear() noexcept {
    published_.store(0, std::memory_order_release);
    producer_generation_ = 0;
    for (auto& slot : slots_) {
        if (slot.readers.load(std::memory_order_acquire) == 0) {
            slot.publication = {};
            slot.size = 0;
            slot.generation = 0;
            slot.consumed.store(0, std::memory_order_release);
        }
    }
}

Snapshot Cache::snapshot() const noexcept {
    const auto published = published_.load(std::memory_order_acquire);
    return Snapshot{
        unpack_generation(published),
        publishes_.load(std::memory_order_relaxed),
        publish_blocked_.load(std::memory_order_relaxed),
        hits_.load(std::memory_order_relaxed),
        empty_.load(std::memory_order_relaxed),
        mismatch_.load(std::memory_order_relaxed),
        stale_.load(std::memory_order_relaxed),
        consumed_.load(std::memory_order_relaxed),
        changed_.load(std::memory_order_relaxed),
    };
}

} // namespace pm::v7::prepared_order
