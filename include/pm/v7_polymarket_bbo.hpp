#pragma once

#include "pm/v7_spsc.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <span>
#include <string>
#include <string_view>
#include <type_traits>
#include <vector>

namespace pm::v7::polymarket_bbo {

enum class SourceKind : std::uint8_t {
    PriceChange = 1,
    BestBidAsk = 2,
};

struct Binding {
    std::string asset_id;
    std::uint64_t market_handle = 0;
    std::uint64_t event_handle = 0;
    std::uint64_t instrument_handle = 0;
};

// Price-only top-of-book event for the reaction path. Queue/depth state remains
// owned by the canonical full L2 path; this object intentionally does not copy
// BookHotSnapshot or reconstruct depth.
struct Update {
    std::uint64_t market_handle = 0;
    std::uint64_t event_handle = 0;
    std::uint64_t instrument_handle = 0;
    std::int64_t exchange_event_ns = 0;
    std::int64_t receive_monotonic_ns = 0;
    // Stable numeric identity for redundant-feed dedupe. Zero means that the
    // venue event lacked enough exchange identity to deduplicate safely.
    std::uint64_t event_identity = 0;
    std::int32_t best_bid_e4 = 0;
    std::int32_t best_ask_e4 = 0;
    SourceKind source = SourceKind::PriceChange;
    std::uint8_t valid = 0;
};

enum class FirstArrivalDecision : std::uint8_t {
    Accept = 1,
    Duplicate = 2,
    Invalid = 3,
    CollisionAccept = 4,
};

struct FirstArrivalResult {
    FirstArrivalDecision decision = FirstArrivalDecision::Invalid;
    std::int64_t first_receive_monotonic_ns = 0;
    std::int64_t duplicate_delay_ns = 0;
    std::uint8_t connection_mask = 0;
};

// Single-owner gate for N redundant public PM feeds. Callers must causally merge
// source queue heads by receive_monotonic_ns before observe(). It never rejects
// a distinct BBO identity; table pressure accepts untracked rather than dropping.
class FirstArrivalGate final {
public:
    explicit FirstArrivalGate(std::int64_t duplicate_window_ns = 500'000'000LL) noexcept;
    [[nodiscard]] FirstArrivalResult observe(
        const Update& update, std::uint8_t connection_slot) noexcept;
    void reset() noexcept;

private:
    struct Entry {
        std::uint64_t identity = 0;
        std::int64_t first_receive_monotonic_ns = 0;
        std::int64_t last_receive_monotonic_ns = 0;
        std::uint8_t connection_mask = 0;
    };
    static constexpr std::size_t kCapacity = 4096;
    static constexpr std::size_t kMask = kCapacity - 1;
    std::array<Entry, kCapacity> entries_{};
    std::int64_t duplicate_window_ns_ = 500'000'000LL;
};

template <std::size_t SourceCount = 3, std::size_t QueueCapacity = 2048>
class RedundantIngress final {
    static_assert(SourceCount >= 2 && SourceCount <= 8);
public:
    explicit RedundantIngress(std::int64_t duplicate_window_ns = 500'000'000LL) noexcept
        : gate_(duplicate_window_ns) {}

    [[nodiscard]] bool try_push(std::uint8_t source, const Update& update) noexcept {
        return source < SourceCount && update.valid != 0
            && queues_[source].try_push(update);
    }

    // Bounded duplicate draining keeps one call from monopolizing the decision
    // owner after a burst. A false result can mean empty or duplicate-only; the
    // caller may call again immediately before sleeping.
    [[nodiscard]] bool try_pop_first(Update& output, std::uint8_t& source) noexcept {
        constexpr std::size_t kDuplicateDrainBudget = SourceCount * 8;
        for (std::size_t drained = 0; drained <= kDuplicateDrainBudget; ++drained) {
            for (std::size_t i = 0; i < SourceCount; ++i) {
                if (pending_valid_[i] == 0 && queues_[i].try_pop(pending_[i])) {
                    pending_valid_[i] = 1;
                }
            }
            std::size_t best = SourceCount;
            for (std::size_t i = 0; i < SourceCount; ++i) {
                if (pending_valid_[i] == 0) continue;
                if (best == SourceCount
                    || pending_[i].receive_monotonic_ns < pending_[best].receive_monotonic_ns
                    || (pending_[i].receive_monotonic_ns == pending_[best].receive_monotonic_ns
                        && i < best)) {
                    best = i;
                }
            }
            if (best == SourceCount) return false;
            const Update& candidate = pending_[best];
            pending_valid_[best] = 0;
            const auto decision = gate_.observe(candidate, static_cast<std::uint8_t>(best));
            if (decision.decision == FirstArrivalDecision::Duplicate) {
                ++duplicate_drops_;
                continue;
            }
            if (decision.decision == FirstArrivalDecision::Invalid) {
                ++invalid_drops_;
                continue;
            }
            if (decision.decision == FirstArrivalDecision::CollisionAccept) {
                ++collision_accepts_;
            }
            output = candidate;
            source = static_cast<std::uint8_t>(best);
            return true;
        }
        ++duplicate_budget_exhaustions_;
        return false;
    }

    [[nodiscard]] std::uint64_t duplicate_drops() const noexcept { return duplicate_drops_; }
    [[nodiscard]] std::uint64_t invalid_drops() const noexcept { return invalid_drops_; }
    [[nodiscard]] std::uint64_t collision_accepts() const noexcept { return collision_accepts_; }
    [[nodiscard]] std::uint64_t duplicate_budget_exhaustions() const noexcept {
        return duplicate_budget_exhaustions_;
    }

private:
    std::array<pm::v7::SpscRing<Update, QueueCapacity>, SourceCount> queues_{};
    std::array<Update, SourceCount> pending_{};
    std::array<std::uint8_t, SourceCount> pending_valid_{};
    FirstArrivalGate gate_;
    std::uint64_t duplicate_drops_ = 0;
    std::uint64_t invalid_drops_ = 0;
    std::uint64_t collision_accepts_ = 0;
    std::uint64_t duplicate_budget_exhaustions_ = 0;
};

struct FrameResult {
    std::size_t output_count = 0;
    std::size_t recognized_updates = 0;
    std::size_t unknown_assets = 0;
    std::size_t incomplete_bbo = 0;
    std::uint8_t invalid_frame = 0;
    std::uint8_t output_overflow = 0;
};

class Decoder final {
public:
    explicit Decoder(std::vector<Binding> bindings);

    // Decodes only the venue-provided BBO surfaces used by the decision path:
    // `price_change[].best_bid/best_ask` and `best_bid_ask`. Full book snapshots,
    // trades, tick changes and recovery remain with MarketWsShard.
    [[nodiscard]] FrameResult decode(
        std::string_view payload,
        std::int64_t receive_monotonic_ns,
        std::span<Update> output) const noexcept;

private:
    [[nodiscard]] const Binding* binding(std::string_view asset_id) const noexcept;
    std::vector<Binding> bindings_;
};

static_assert(std::is_trivially_copyable_v<Update>);
static_assert(std::is_standard_layout_v<Update>);
static_assert(sizeof(Update) <= 64);

} // namespace pm::v7::polymarket_bbo
