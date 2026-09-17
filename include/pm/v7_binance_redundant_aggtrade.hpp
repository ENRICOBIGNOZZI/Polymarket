#pragma once

#include "pm/v7_external_fair.hpp"

#include <array>
#include <bit>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <type_traits>

namespace pm::v7::external_fair {

inline constexpr std::size_t kBinanceAggTradeRedundantLanes = 3;
inline constexpr std::size_t kBinanceAggTradeMaxAssets = 8;
inline constexpr std::size_t kBinanceAggTradeRecent = 64;

struct RedundantAggTradeResult {
    std::uint64_t asset_handle = 0;
    std::uint64_t source_sequence = 0;
    std::int64_t first_receive_monotonic_ns = 0;
    std::int64_t receive_monotonic_ns = 0;
    std::int64_t first_to_quorum_ns = 0;
    std::uint8_t lane = 0;
    std::uint8_t first_arrival_shadow = 0;
    std::uint8_t duplicate = 0;
    std::uint8_t quorum_confirmed = 0;
    std::uint8_t conflict = 0;
    std::uint8_t stale = 0;
    std::uint8_t lane_unhealthy = 0;
    std::uint8_t asset_poisoned = 0;
    std::uint8_t causal_order_violation = 0;
    std::uint8_t invalid = 0;
};

class RedundantBinanceAggTradeShadowGate final {
public:
    [[nodiscard]] bool register_asset(std::uint64_t asset_handle) noexcept {
        if (asset_handle == 0) return false;
        if (find_asset(asset_handle) != nullptr) return true;
        for (auto& asset : assets_) {
            if (asset.registered == 0) {
                asset = AssetState{};
                asset.asset_handle = asset_handle;
                asset.registered = 1;
                return true;
            }
        }
        return false;
    }

    [[nodiscard]] RedundantAggTradeResult on_event(
        std::uint8_t lane,
        const ExternalVenueEvent& event) noexcept {
        RedundantAggTradeResult out{};
        out.asset_handle = event.asset_handle;
        out.source_sequence = event.source_sequence;
        out.receive_monotonic_ns = event.local_receive_monotonic_ns;
        out.lane = lane;
        auto* asset = find_asset(event.asset_handle);
        if (lane >= lanes_.size() || asset == nullptr
            || event.venue != VenueId::BinanceSpot
            || event.event_type != ExternalEventType::Trade
            || event.connection_epoch == 0 || event.source_sequence == 0
            || event.local_receive_monotonic_ns <= 0
            || event.exchange_event_ns <= 0
            || !std::isfinite(event.trade_price) || event.trade_price <= 0.0
            || !std::isfinite(event.trade_size) || event.trade_size <= 0.0
            || (event.trade_side != 1 && event.trade_side != -1)) {
            out.invalid = 1;
            return out;
        }

        auto& lane_state = lanes_[lane];
        if (event.gap != 0 || event.healthy == 0) {
            lane_state.healthy = 0;
            out.lane_unhealthy = 1;
            return out;
        }
        if (!accept_epoch(lane_state, event.connection_epoch)) {
            out.lane_unhealthy = lane_state.healthy == 0 ? 1 : 0;
            out.stale = lane_state.healthy != 0 ? 1 : 0;
            return out;
        }
        if (lane_state.healthy == 0) {
            out.lane_unhealthy = 1;
            return out;
        }
        if (event.local_receive_monotonic_ns < lane_state.last_receive_ns) {
            lane_state.healthy = 0;
            out.lane_unhealthy = 1;
            return out;
        }
        lane_state.last_receive_ns = event.local_receive_monotonic_ns;

        auto& last_sequence = asset->lane_last_sequence[lane];
        if (event.source_sequence < last_sequence) {
            out.stale = 1;
            return out;
        }

        const Fingerprint fingerprint = fingerprint_of(event);
        auto& recent = asset->recent[event.source_sequence % asset->recent.size()];
        if (event.source_sequence == last_sequence) {
            if (recent.sequence == event.source_sequence) {
                handle_duplicate(*asset, recent, lane, fingerprint, event, out);
            } else {
                out.stale = 1;
            }
            return out;
        }
        last_sequence = event.source_sequence;

        if (recent.sequence == event.source_sequence) {
            handle_duplicate(*asset, recent, lane, fingerprint, event, out);
            return out;
        }
        if (event.source_sequence <= asset->highest_emitted_sequence) {
            out.stale = 1;
            return out;
        }
        recent = RecentEntry{};
        recent.sequence = event.source_sequence;
        recent.fingerprint = fingerprint;
        recent.first_receive_ns = event.local_receive_monotonic_ns;
        recent.seen_lane_mask = static_cast<std::uint8_t>(1U << lane);
        asset->highest_emitted_sequence = event.source_sequence;
        out.first_receive_monotonic_ns = recent.first_receive_ns;
        if (asset->poisoned != 0) {
            out.asset_poisoned = 1;
            return out;
        }
        out.first_arrival_shadow = 1;
        return out;
    }

    [[nodiscard]] bool mark_lane_unhealthy(
        std::uint8_t lane, std::uint64_t connection_epoch) noexcept {
        if (lane >= lanes_.size() || connection_epoch == 0) return false;
        auto& state = lanes_[lane];
        if (state.connection_epoch != 0
            && connection_epoch < state.connection_epoch) return false;
        state.connection_epoch = connection_epoch;
        state.healthy = 0;
        return true;
    }

    [[nodiscard]] bool mark_lane_recovered(
        std::uint8_t lane, std::uint64_t connection_epoch) noexcept {
        if (lane >= lanes_.size() || connection_epoch == 0) return false;
        auto& state = lanes_[lane];
        if (state.connection_epoch != 0
            && connection_epoch < state.connection_epoch) return false;
        state.connection_epoch = connection_epoch;
        state.last_receive_ns = 0;
        state.healthy = 1;
        for (auto& asset : assets_) {
            if (asset.registered != 0) asset.lane_last_sequence[lane] = 0;
        }
        return true;
    }

    [[nodiscard]] bool mark_asset_recovered(
        std::uint64_t asset_handle,
        std::uint64_t baseline_sequence) noexcept {
        auto* asset = find_asset(asset_handle);
        if (asset == nullptr || baseline_sequence == 0) return false;
        asset->poisoned = 0;
        asset->highest_emitted_sequence = baseline_sequence;
        asset->recent = {};
        for (auto& sequence : asset->lane_last_sequence) {
            sequence = baseline_sequence;
        }
        return true;
    }

    [[nodiscard]] bool asset_poisoned(std::uint64_t asset_handle) const noexcept {
        const auto* asset = find_asset(asset_handle);
        return asset != nullptr && asset->poisoned != 0;
    }

private:
    struct Fingerprint {
        std::int64_t exchange_event_ns = 0;
        std::uint64_t price_bits = 0;
        std::uint64_t size_bits = 0;
        std::int8_t trade_side = 0;

        [[nodiscard]] bool operator==(const Fingerprint& other) const noexcept {
            return exchange_event_ns == other.exchange_event_ns
                && price_bits == other.price_bits
                && size_bits == other.size_bits
                && trade_side == other.trade_side;
        }
    };

    struct RecentEntry {
        std::uint64_t sequence = 0;
        Fingerprint fingerprint{};
        std::int64_t first_receive_ns = 0;
        std::uint8_t seen_lane_mask = 0;
        std::uint8_t quorum_reported = 0;
    };

    struct AssetState {
        std::uint64_t asset_handle = 0;
        std::uint64_t highest_emitted_sequence = 0;
        std::array<std::uint64_t, kBinanceAggTradeRedundantLanes>
            lane_last_sequence{};
        std::array<RecentEntry, kBinanceAggTradeRecent> recent{};
        std::uint8_t registered = 0;
        std::uint8_t poisoned = 0;
    };
    struct LaneState {
        std::uint64_t connection_epoch = 0;
        std::int64_t last_receive_ns = 0;
        std::uint8_t healthy = 1;
    };

    [[nodiscard]] static Fingerprint fingerprint_of(
        const ExternalVenueEvent& event) noexcept {
        Fingerprint out{};
        out.exchange_event_ns = event.exchange_event_ns;
        out.price_bits = std::bit_cast<std::uint64_t>(event.trade_price);
        out.size_bits = std::bit_cast<std::uint64_t>(event.trade_size);
        out.trade_side = event.trade_side;
        return out;
    }

    [[nodiscard]] static bool accept_epoch(
        LaneState& lane,
        std::uint64_t connection_epoch) noexcept {
        if (lane.connection_epoch == 0) {
            lane.connection_epoch = connection_epoch;
            lane.healthy = 1;
            return true;
        }
        if (connection_epoch < lane.connection_epoch) return false;
        if (connection_epoch > lane.connection_epoch) {
            lane.connection_epoch = connection_epoch;
            lane.last_receive_ns = 0;
            lane.healthy = 0;
            return false;
        }
        return true;
    }
    static void handle_duplicate(
        AssetState& asset,
        RecentEntry& recent,
        std::uint8_t lane,
        const Fingerprint& fingerprint,
        const ExternalVenueEvent& event,
        RedundantAggTradeResult& out) noexcept {
        out.first_receive_monotonic_ns = recent.first_receive_ns;
        if (event.local_receive_monotonic_ns < recent.first_receive_ns) {
            asset.poisoned = 1;
            out.causal_order_violation = 1;
            out.asset_poisoned = 1;
            return;
        }
        if (!(recent.fingerprint == fingerprint)) {
            asset.poisoned = 1;
            out.conflict = 1;
            out.asset_poisoned = 1;
            return;
        }
        out.duplicate = 1;
        const auto lane_bit = static_cast<std::uint8_t>(1U << lane);
        if ((recent.seen_lane_mask & lane_bit) == 0) {
            recent.seen_lane_mask = static_cast<std::uint8_t>(
                recent.seen_lane_mask | lane_bit);
            if (recent.quorum_reported == 0
                && std::popcount(static_cast<unsigned>(recent.seen_lane_mask)) >= 2) {
                recent.quorum_reported = 1;
                out.quorum_confirmed = 1;
                out.first_to_quorum_ns =
                    event.local_receive_monotonic_ns - recent.first_receive_ns;
            }
        }
        if (asset.poisoned != 0) out.asset_poisoned = 1;
    }
    [[nodiscard]] AssetState* find_asset(std::uint64_t asset_handle) noexcept {
        for (auto& asset : assets_) {
            if (asset.registered != 0 && asset.asset_handle == asset_handle) {
                return &asset;
            }
        }
        return nullptr;
    }

    [[nodiscard]] const AssetState* find_asset(
        std::uint64_t asset_handle) const noexcept {
        for (const auto& asset : assets_) {
            if (asset.registered != 0 && asset.asset_handle == asset_handle) {
                return &asset;
            }
        }
        return nullptr;
    }

    std::array<AssetState, kBinanceAggTradeMaxAssets> assets_{};
    std::array<LaneState, kBinanceAggTradeRedundantLanes> lanes_{};
};

static_assert(std::is_trivially_copyable_v<RedundantAggTradeResult>);
static_assert(std::is_standard_layout_v<RedundantAggTradeResult>);

} // namespace pm::v7::external_fair
