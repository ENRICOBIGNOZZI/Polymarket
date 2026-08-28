#pragma once

#include "pm/v7_intent.hpp"

#include <array>
#include <atomic>
#include <bit>
#include <cstddef>
#include <type_traits>

namespace pm::v7 {

template <class T, std::size_t Capacity>
class SpscRing final {
    static_assert(Capacity >= 2);
    static_assert(std::has_single_bit(Capacity), "SPSC capacity must be a power of two");
    static_assert(std::is_trivially_copyable_v<T>, "hot-path SPSC payload must be trivially copyable");

public:
    [[nodiscard]] bool try_push(const T& value) noexcept {
        const std::size_t head = head_.load(std::memory_order_relaxed);
        const std::size_t tail = tail_.load(std::memory_order_acquire);
        if (head - tail >= Capacity) return false;
        storage_[head & mask_] = value;
        head_.store(head + 1, std::memory_order_release);
        return true;
    }

    [[nodiscard]] bool try_pop(T& value) noexcept {
        const std::size_t tail = tail_.load(std::memory_order_relaxed);
        const std::size_t head = head_.load(std::memory_order_acquire);
        if (tail == head) return false;
        value = storage_[tail & mask_];
        tail_.store(tail + 1, std::memory_order_release);
        return true;
    }

    [[nodiscard]] std::size_t approximate_size() const noexcept {
        const std::size_t head = head_.load(std::memory_order_acquire);
        const std::size_t tail = tail_.load(std::memory_order_acquire);
        return head - tail;
    }

    [[nodiscard]] constexpr std::size_t capacity() const noexcept { return Capacity; }

private:
    static constexpr std::size_t mask_ = Capacity - 1;
    std::array<T, Capacity> storage_{};
    alignas(64) std::atomic<std::size_t> head_{0};
    alignas(64) std::atomic<std::size_t> tail_{0};
};

template <std::size_t CriticalCapacity = 256, std::size_t NormalCapacity = 2048>
class PriorityIntentQueue final {
public:
    [[nodiscard]] bool try_push(const StrategyIntent& intent) noexcept {
        if (is_critical_intent(intent.type) || intent.urgency == Urgency::Critical) {
            return critical_.try_push(intent);
        }
        return normal_.try_push(intent);
    }

    // Risk-off traffic is always drained before new quote traffic.
    [[nodiscard]] bool try_pop(StrategyIntent& intent) noexcept {
        if (critical_.try_pop(intent)) return true;
        return normal_.try_pop(intent);
    }

    [[nodiscard]] std::size_t critical_backlog() const noexcept {
        return critical_.approximate_size();
    }

    [[nodiscard]] std::size_t normal_backlog() const noexcept {
        return normal_.approximate_size();
    }

private:
    SpscRing<StrategyIntent, CriticalCapacity> critical_{};
    SpscRing<StrategyIntent, NormalCapacity> normal_{};
};

} // namespace pm::v7
