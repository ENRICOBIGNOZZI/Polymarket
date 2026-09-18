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

struct NativeRuntimeEvidenceConfig {
    std::string run_root;
    std::string model_sha;
    std::string run_id;
    std::string server_id;
    std::string market_id;
    std::string event_id;
    std::string yes_token_id;
    std::string no_token_id;
    std::string fee_source;
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
    std::unique_ptr<Impl> impl_;
    std::atomic<bool> healthy_{true};
    std::atomic<bool> stopping_{false};
    std::atomic<std::uint64_t> published_{0};
    std::atomic<std::uint64_t> written_{0};
    std::atomic<std::uint64_t> dropped_{0};
};

} // namespace pm::v7
