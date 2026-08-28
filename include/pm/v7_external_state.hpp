#pragma once

#include "pm/v7_external_fair.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <type_traits>

namespace pm::v7::external_fair {

struct ExternalStatePolicy {
    std::array<double, kVenueCount> venue_weights{0.34, 0.33, 0.33};
    std::int64_t max_venue_age_ns = 1'000'000'000LL;
    std::uint32_t min_healthy_venues = 2;
    double vol_fast_alpha = 0.20;
    double vol_medium_alpha = 0.05;
    double vol_slow_alpha = 0.01;
    double flow_alpha = 0.10;
};

struct CausalStateInputs {
    std::uint64_t pm_state_version = 0;
    std::uint64_t oracle_state_version = 0;
    std::uint64_t external_state_version = 0;
    std::uint64_t settlement_reference_version = 0;
    std::uint64_t inventory_state_version = 0;
    std::uint64_t private_state_version = 0;
    std::uint64_t risk_state_version = 0;
    std::int64_t pm_receive_ns = 0;
    std::int64_t oracle_receive_ns = 0;
    std::int64_t external_receive_ns = 0;
    std::int64_t settlement_reference_receive_ns = 0;
    std::int64_t inventory_receive_ns = 0;
    std::int64_t private_receive_ns = 0;
    std::int64_t risk_receive_ns = 0;
};

class OracleState final {
public:
    OracleState() noexcept = default;

    // Single-writer. A gap/reconnect invalidates continuity. A subsequent
    // same-oracle recovery snapshot is explicitly labelled recovered rather
    // than being silently treated as uninterrupted live history.
    [[nodiscard]] bool on_event(const OracleEvent& event) noexcept;
    [[nodiscard]] OracleSnapshot snapshot(std::int64_t now_ns,
                                          std::int64_t max_age_ns) const noexcept;
    [[nodiscard]] std::uint64_t state_version() const noexcept { return state_version_; }

private:
    OracleEvent last_{};
    std::uint64_t state_version_ = 0;
    std::uint64_t previous_sequence_ = 0;
    std::uint64_t previous_epoch_ = 0;
    std::uint8_t consecutive_clean_ = 0;
    OracleContinuity continuity_ = OracleContinuity::ContinuityUnknown;
};

class ExternalAssetState final {
public:
    explicit ExternalAssetState(std::uint64_t asset_handle = 0) noexcept;

    // One owner thread mutates this object. Venue IO threads hand POD events to
    // this owner through bounded SPSC queues.
    [[nodiscard]] bool on_venue_event(const ExternalVenueEvent& event,
                                      const ExternalStatePolicy& policy) noexcept;
    void on_oracle_snapshot(const OracleSnapshot& oracle) noexcept;
    [[nodiscard]] ExternalAssetSnapshot snapshot(std::int64_t now_ns,
                                                 const ExternalStatePolicy& policy) const noexcept;
    [[nodiscard]] std::uint64_t state_version() const noexcept { return state_version_; }

private:
    struct VenueState {
        std::uint64_t source_sequence = 0;
        std::uint64_t connection_epoch = 0;
        std::int64_t last_receive_ns = 0;
        double bid = 0.0;
        double ask = 0.0;
        double bid_size = 0.0;
        double ask_size = 0.0;
        double mid = 0.0;
        double microprice = 0.0;
        double previous_bid_size = 0.0;
        double previous_ask_size = 0.0;
        double signed_trade_flow = 0.0;
        double ofi = 0.0;
        std::uint8_t healthy = 0;
        std::uint8_t gap = 0;
        std::uint8_t valid = 0;
        std::uint8_t reserved = 0;
    };

    struct PriceSample {
        std::int64_t receive_ns = 0;
        double price = 0.0;
    };

    [[nodiscard]] static std::size_t venue_index(VenueId venue) noexcept;
    [[nodiscard]] double compute_composite(std::int64_t now_ns,
                                           const ExternalStatePolicy& policy,
                                           double* microprice,
                                           double* dispersion_bps,
                                           std::uint32_t* health_mask,
                                           std::uint32_t* fresh_count) const noexcept;
    [[nodiscard]] double lagged_return(std::int64_t now_ns,
                                       std::int64_t horizon_ns,
                                       double current_price) const noexcept;
    void record_price_sample(std::int64_t receive_ns, double price) noexcept;

    std::uint64_t asset_handle_ = 0;
    std::uint64_t state_version_ = 0;
    std::array<VenueState, kVenueCount> venues_{};
    OracleSnapshot oracle_{};
    std::array<PriceSample, 256> history_{};
    std::size_t history_head_ = 0;
    std::size_t history_count_ = 0;
    double ew_var_fast_ = 0.0;
    double ew_var_medium_ = 0.0;
    double ew_var_slow_ = 0.0;
    double aggregate_ofi_ = 0.0;
    double aggregate_trade_imbalance_ = 0.0;
    double last_composite_ = 0.0;
    std::int64_t latest_receive_ns_ = 0;
};

[[nodiscard]] CausalCut make_causal_cut(
    std::uint64_t causal_cut_id,
    std::int64_t decision_monotonic_ns,
    TriggerSource trigger_source,
    std::uint64_t trigger_source_version,
    const CausalStateInputs& inputs) noexcept;

static_assert(std::is_trivially_copyable_v<ExternalStatePolicy>);
static_assert(std::is_trivially_copyable_v<CausalStateInputs>);

} // namespace pm::v7::external_fair
