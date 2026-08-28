#pragma once

#include "pm/v7_spsc.hpp"

#include <array>
#include <bit>
#include <cstddef>
#include <cstdint>

namespace pm::v7 {

struct OrderTxBacklog {
    std::size_t critical = 0;
    std::size_t normal = 0;
};

// Each hot shard is the sole producer of its own channel; exactly one order-TX
// core is the consumer of every channel. This preserves SPSC semantics while
// providing one global arbitration point for capital/risk/OMS. The consumer
// scans *all* critical lanes before any normal quote lane, so a CANCEL/KILL from
// shard N cannot sit behind fresh quotes from shard 0.
template <std::size_t MaxShards = 16,
          std::size_t CriticalCapacity = 256,
          std::size_t NormalCapacity = 2048>
class OrderTxArbiter final {
    static_assert(MaxShards >= 1);
    static_assert(std::has_single_bit(CriticalCapacity));
    static_assert(std::has_single_bit(NormalCapacity));

public:
    explicit OrderTxArbiter(std::size_t active_shards = 1) noexcept
        : active_shards_(active_shards >= 1 && active_shards <= MaxShards
              ? active_shards : 1) {}

    // Producer API: shard_id must be stable and each shard_id must be written by
    // exactly one producer thread. Full queues reject instead of growing.
    [[nodiscard]] bool try_push(std::size_t shard_id,
                                const StrategyIntent& intent) noexcept {
        if (shard_id >= active_shards_) return false;
        auto& channel = channels_[shard_id];
        if (is_critical_intent(intent.type) || intent.urgency == Urgency::Critical) {
            return channel.critical.try_push(intent);
        }
        return channel.normal.try_push(intent);
    }

    // Single-consumer API. Critical and normal cursors are independently
    // round-robin so no busy shard can permanently starve another.
    [[nodiscard]] bool try_pop(StrategyIntent& intent,
                               std::size_t& source_shard) noexcept {
        for (std::size_t step = 0; step < active_shards_; ++step) {
            const std::size_t shard = (critical_cursor_ + step) % active_shards_;
            if (channels_[shard].critical.try_pop(intent)) {
                source_shard = shard;
                critical_cursor_ = (shard + 1) % active_shards_;
                return true;
            }
        }
        for (std::size_t step = 0; step < active_shards_; ++step) {
            const std::size_t shard = (normal_cursor_ + step) % active_shards_;
            if (channels_[shard].normal.try_pop(intent)) {
                source_shard = shard;
                normal_cursor_ = (shard + 1) % active_shards_;
                return true;
            }
        }
        return false;
    }

    [[nodiscard]] OrderTxBacklog backlog() const noexcept {
        OrderTxBacklog out;
        for (std::size_t shard = 0; shard < active_shards_; ++shard) {
            out.critical += channels_[shard].critical.approximate_size();
            out.normal += channels_[shard].normal.approximate_size();
        }
        return out;
    }

    [[nodiscard]] std::size_t active_shards() const noexcept { return active_shards_; }

private:
    struct Channel {
        SpscRing<StrategyIntent, CriticalCapacity> critical{};
        SpscRing<StrategyIntent, NormalCapacity> normal{};
    };

    std::array<Channel, MaxShards> channels_{};
    std::size_t active_shards_ = 1;
    std::size_t critical_cursor_ = 0;
    std::size_t normal_cursor_ = 0;
};

} // namespace pm::v7
