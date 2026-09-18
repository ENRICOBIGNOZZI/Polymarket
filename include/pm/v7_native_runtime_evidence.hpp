#pragma once

#include "pm/v7_execution_plan.hpp"
#include "pm/v7_native_order_tx.hpp"
#include "pm/v7_native_paper_execution.hpp"
#include "pm/v7_spsc.hpp"

#include <atomic>
#include <cstdint>
#include <memory>
#include <string>

namespace pm::v7 {

enum class NativeEvidenceKind : std::uint8_t {
    OrderSubmitted = 1,
    OrderState = 2,
    Fill = 3,
};

struct NativeEvidenceEvent {
    NativeEvidenceKind kind = NativeEvidenceKind::OrderSubmitted;
    NativeOrderCommand command{};
    NativePaperFillRecord fill{};
    StrategyId strategy_id = StrategyId::CryptoSettlementEngine;
    ExecutionPolicyId policy = ExecutionPolicyId::AggressiveTaker;
    OrderState order_state = OrderState::Unknown;
    std::int64_t causal_exchange_event_ns = 0;
    std::int64_t causal_receive_monotonic_ns = 0;
    std::int64_t recorded_monotonic_ns = 0;
};

// A bounded copy of exactly the state consumed by the native decision owner.
// Research observations never enter the economic ledger or order transport.
struct NativeObservation {
    std::uint64_t instrument_handle = 0, book_version = 0, signal_version = 0;
    std::int64_t receive_ns = 0, exchange_ns = 0, observed_ns = 0, trigger_ns = 0;
    std::int64_t decision_ns = 0, close_ns = 0;
    std::int64_t bid_quantity = 0, ask_quantity = 0, trade_quantity = 0;
    std::int32_t bid_e4 = 0, ask_e4 = 0, tick_e4 = 0, trade_e4 = 0;
    std::array<std::int32_t, 10> bid_prices{}, ask_prices{};
    std::array<std::int64_t, 10> bid_quantities{}, ask_quantities{};
    std::int64_t event_receive_ns = 0, event_exchange_ns = 0;
    std::uint8_t event_kind = 0;
    double signal_return_bp = 0.0, confirmation_return_bp = 0.0;
    double expected_ev = 0.0, ev_uncertainty = 0.0;
    std::int64_t proposed_quantity = 0, proposed_price_tick = 0;
    std::uint64_t connection_epoch = 0;
    std::int8_t direction = 0;
    std::uint8_t kind = 0, reason = 0, valid = 0, accepted = 0, trade_side = 0;
    std::uint8_t signal_valid = 0, confirmed_non_opposing = 0;
};

struct NativeRuntimeEvidenceConfig {
    std::string run_root;
    std::string model_sha;
    std::string run_id;
    std::string server_id;
    std::string asset;
    std::string horizon;
    std::string market_id;
    std::string event_id;
    std::string yes_token_id;
    std::string no_token_id;
    std::string fee_source;
    std::uint64_t maker_valid_cells = 0;
    std::int64_t minimum_order_microunits = 0;
    std::string risk_policy_sha256;
    std::string maker_artifact_sha256, maker_policy_sha256;
    std::uint64_t yes_instrument_handle = 0;
    std::uint64_t no_instrument_handle = 0;
    std::int64_t close_wall_ns = 0;
    double taker_fee_rate = 0.0;
    double taker_fee_exponent = 1.0;
    std::uint8_t taker_only_fee = 1;

    [[nodiscard]] bool valid() const noexcept;
};

// Cold/off-thread evidence transport for the sole native engine. publish() is
// allocation-free and filesystem-free; one dedicated writer thread serializes
// canonical LedgerEvent-compatible JSON into the existing single-writer spool.
class NativeRuntimeEvidenceWriter final {
public:
    explicit NativeRuntimeEvidenceWriter(NativeRuntimeEvidenceConfig config);
    ~NativeRuntimeEvidenceWriter();

    NativeRuntimeEvidenceWriter(const NativeRuntimeEvidenceWriter&) = delete;
    NativeRuntimeEvidenceWriter& operator=(const NativeRuntimeEvidenceWriter&) = delete;

    [[nodiscard]] bool publish(const NativeEvidenceEvent& event) noexcept;
    [[nodiscard]] bool publish_observation(const NativeObservation& event) noexcept;
    void stop() noexcept;

    [[nodiscard]] bool healthy() const noexcept {
        return healthy_.load(std::memory_order_acquire);
    }
    [[nodiscard]] std::uint64_t published() const noexcept {
        return published_.load(std::memory_order_acquire);
    }
    [[nodiscard]] std::uint64_t written() const noexcept {
        return written_.load(std::memory_order_acquire);
    }
    [[nodiscard]] std::uint64_t dropped() const noexcept {
        return dropped_.load(std::memory_order_acquire);
    }

private:
    static constexpr std::size_t kQueueCapacity = 4096;
    struct Impl;
    SpscRing<NativeEvidenceEvent, kQueueCapacity> queue_{};
    std::unique_ptr<SpscRing<NativeObservation, 8192>> observations_;
    std::unique_ptr<Impl> impl_;
    std::atomic<std::uint64_t> observations_published_{0}, observations_written_{0}, observations_dropped_{0};
    std::atomic<bool> healthy_{true};
    std::atomic<bool> stopping_{false};
    std::atomic<std::uint64_t> published_{0};
    std::atomic<std::uint64_t> written_{0};
    std::atomic<std::uint64_t> dropped_{0};
};

} // namespace pm::v7
