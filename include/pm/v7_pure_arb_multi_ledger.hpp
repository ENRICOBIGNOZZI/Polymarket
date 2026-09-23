#pragma once

#include "pm/v7_native_paper_execution.hpp"
#include "pm/v7_spsc.hpp"

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace pm::v7 {

struct PureArbLedgerMarket {
    std::string market_id;
    std::string event_id;
    std::string asset;
    std::string horizon;
    std::string yes_token;
    std::string no_token;
    std::string fee_source;
    std::uint64_t yes_instrument_handle = 0;
    std::uint64_t no_instrument_handle = 0;
    double fee_rate = 0.0;
    double fee_exponent = 1.0;
};

struct PureArbMultiLedgerConfig {
    std::string run_root;
    std::string model_sha;
    std::string run_id;
    std::string server_id;
    std::string risk_policy_sha256;
    std::vector<PureArbLedgerMarket> markets;
};

struct PureArbMultiLedgerSnapshot {
    std::uint64_t published = 0;
    std::uint64_t written = 0;
    std::uint64_t dropped = 0;
    std::uint64_t queued = 0;
    std::uint8_t healthy = 0;
};

// Single producer is the one PM WebSocket callback.  The writer thread owns
// all JSON/filesystem work and publishes LedgerEvent-compatible FILL records
// to the existing single-writer spool.
class PureArbMultiLedgerWriter final {
public:
    explicit PureArbMultiLedgerWriter(PureArbMultiLedgerConfig config);
    ~PureArbMultiLedgerWriter();

    PureArbMultiLedgerWriter(const PureArbMultiLedgerWriter&) = delete;
    PureArbMultiLedgerWriter& operator=(const PureArbMultiLedgerWriter&) = delete;

    [[nodiscard]] bool publish(
        std::size_t context_index,
        const NativePaperFillRecord& fill) noexcept;
    void stop() noexcept;
    [[nodiscard]] PureArbMultiLedgerSnapshot snapshot() const noexcept;

private:
    struct Envelope {
        std::uint16_t context_index = 0;
        std::uint16_t reserved = 0;
        NativePaperFillRecord fill{};
    };
    static constexpr std::size_t kCapacity = 4096;
    struct Impl;
    SpscRing<Envelope, kCapacity> queue_{};
    std::unique_ptr<Impl> impl_;
    std::atomic<bool> stopping_{false};
    std::atomic<bool> healthy_{true};
    std::atomic<std::uint64_t> published_{0}, written_{0}, dropped_{0};
};

} // namespace pm::v7
