#pragma once

#include "pm/v7_intent.hpp"

#include <array>
#include <cstddef>
#include <cstdint>

namespace pm::v7 {

inline constexpr std::size_t kMaxKillInstruments = 1024;
inline constexpr std::size_t kMaxKillMarkets = 256;
inline constexpr std::size_t kMaxKillEvents = 512;
inline constexpr std::size_t kMaxKillStrategies = 32;

enum KillReason : std::uint32_t {
    KillNone = 0,
    InstrumentKilled = 1U << 0U,
    MarketKilled = 1U << 1U,
    EventKilled = 1U << 2U,
    StrategyKilled = 1U << 3U,
    GlobalKilled = 1U << 4U,
    KillInvalidHandle = 1U << 5U,
};

struct KillMutationResult {
    std::uint64_t state_version = 0;
    std::uint8_t accepted = 0;
    std::uint8_t changed = 0;
};

struct KillDecision {
    std::uint64_t state_version = 0;
    std::uint32_t reason_mask = KillNone;
    std::uint8_t killed = 0;
};

// Common V7 single-writer kill plane.
//
// The execution/risk owner is the only writer. Mutations are fixed-capacity,
// allocation-free, mutex-free and idempotent. Operator/control threads must hand
// mutations to that owner rather than writing this object concurrently. Accepted
// mutations expose a monotonically increasing state_version so the cold control
// plane can persist an audit record without adding I/O to the hot path.
//
// Handles are canonical V7 numeric handles and therefore must be non-zero and
// fit the fixed runtime registry. Unknown/out-of-range handles fail closed at
// evaluation time rather than silently bypassing a kill gate.
class KillSwitchPlane final {
public:
    [[nodiscard]] KillMutationResult set_instrument(std::uint64_t instrument_handle,
                                                    bool killed) noexcept {
        return set_entry(instruments_, instrument_handle, killed);
    }

    [[nodiscard]] KillMutationResult set_market(std::uint64_t market_handle,
                                                bool killed) noexcept {
        return set_entry(markets_, market_handle, killed);
    }

    [[nodiscard]] KillMutationResult set_event(std::uint64_t event_handle,
                                               bool killed) noexcept {
        return set_entry(events_, event_handle, killed);
    }

    [[nodiscard]] KillMutationResult set_strategy(StrategyId strategy_id,
                                                  bool killed) noexcept {
        const auto handle = static_cast<std::uint64_t>(strategy_id);
        return set_entry(strategies_, handle, killed);
    }

    [[nodiscard]] KillMutationResult set_global(bool killed) noexcept {
        KillMutationResult out;
        out.accepted = 1;
        if (global_killed_ == static_cast<std::uint8_t>(killed)) {
            out.state_version = state_version_;
            return out;
        }
        global_killed_ = static_cast<std::uint8_t>(killed);
        bump_version();
        out.changed = 1;
        out.state_version = state_version_;
        return out;
    }

    [[nodiscard]] KillDecision evaluate(StrategyId strategy_id,
                                        std::uint64_t market_handle,
                                        std::uint64_t event_handle,
                                        std::uint64_t instrument_handle) const noexcept {
        KillDecision out;
        out.state_version = state_version_;

        const auto strategy_handle = static_cast<std::uint64_t>(strategy_id);
        if (!valid(instruments_, instrument_handle)
            || !valid(markets_, market_handle)
            || !valid(events_, event_handle)
            || !valid(strategies_, strategy_handle)) {
            out.reason_mask |= KillInvalidHandle;
        }

        if (valid(instruments_, instrument_handle)
            && instruments_[static_cast<std::size_t>(instrument_handle)] != 0) {
            out.reason_mask |= InstrumentKilled;
        }
        if (valid(markets_, market_handle)
            && markets_[static_cast<std::size_t>(market_handle)] != 0) {
            out.reason_mask |= MarketKilled;
        }
        if (valid(events_, event_handle)
            && events_[static_cast<std::size_t>(event_handle)] != 0) {
            out.reason_mask |= EventKilled;
        }
        if (valid(strategies_, strategy_handle)
            && strategies_[static_cast<std::size_t>(strategy_handle)] != 0) {
            out.reason_mask |= StrategyKilled;
        }
        if (global_killed_ != 0) out.reason_mask |= GlobalKilled;

        out.killed = static_cast<std::uint8_t>(out.reason_mask != KillNone);
        return out;
    }

    [[nodiscard]] std::uint64_t state_version() const noexcept { return state_version_; }
    [[nodiscard]] bool global_killed() const noexcept { return global_killed_ != 0; }

private:
    template <std::size_t N>
    [[nodiscard]] static bool valid(const std::array<std::uint8_t, N>&,
                                    std::uint64_t handle) noexcept {
        return handle > 0 && handle < N;
    }

    template <std::size_t N>
    [[nodiscard]] KillMutationResult set_entry(std::array<std::uint8_t, N>& table,
                                               std::uint64_t handle,
                                               bool killed) noexcept {
        KillMutationResult out;
        if (!valid(table, handle)) {
            out.state_version = state_version_;
            return out;
        }
        out.accepted = 1;
        auto& entry = table[static_cast<std::size_t>(handle)];
        const auto next = static_cast<std::uint8_t>(killed);
        if (entry == next) {
            out.state_version = state_version_;
            return out;
        }
        entry = next;
        bump_version();
        out.changed = 1;
        out.state_version = state_version_;
        return out;
    }

    void bump_version() noexcept {
        ++state_version_;
        if (state_version_ == 0) state_version_ = 1;
    }

    std::array<std::uint8_t, kMaxKillInstruments> instruments_{};
    std::array<std::uint8_t, kMaxKillMarkets> markets_{};
    std::array<std::uint8_t, kMaxKillEvents> events_{};
    std::array<std::uint8_t, kMaxKillStrategies> strategies_{};
    std::uint64_t state_version_ = 1;
    std::uint8_t global_killed_ = 0;
};

} // namespace pm::v7
