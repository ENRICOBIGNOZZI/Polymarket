#pragma once

#include "pm/v7_execution_admission.hpp"
#include "pm/v7_external_state.hpp"
#include "pm/v7_market_state.hpp"
#include "pm/v7_probability_ev.hpp"
#include "pm/v7_multirate_context.hpp"

#include <array>
#include <cstdint>
#include <type_traits>

namespace pm::v7 {

enum class NativeCryptoDecisionReason : std::uint8_t {
    Accepted = 1,
    InvalidSignal = 2,
    ExpiredSignal = 3,
    WeakSignal = 4,
    MarketUnavailable = 5,
    TteOutsideWindow = 6,
    InvalidBook = 7,
    InsufficientDepth = 8,
    InvalidTick = 9,
    DuplicateSignal = 10,
    MarketAlreadyTraded = 11,
    CapitalDenied = 12,
    EntryPriceTooHigh = 13,
    MarketAlreadyRepriced = 14,
    ProbabilityUnavailable = 15,
    NetEdgeNonPositive = 16,
    RiskSizeBelowMinimum = 17,
    SlowContextUnavailable = 18,
};
struct NativeCryptoDecisionPolicy {
    std::int64_t minimum_tte_ns = 105'000'000'000LL;
    std::int64_t maximum_tte_ns = 120'000'000'000LL;
    std::int64_t maximum_signal_age_ns = 5'000'000'000LL;
    std::int64_t maximum_book_age_ns = 100'000'000LL;
    double minimum_absolute_binance_return_bp = 0.30;
    std::int64_t target_quantity_microunits = 5'000'000;
    // Optional capital-based PAPER sizing. When positive and probability EV
    // sizing is disabled, quantity is derived from this notional ceiling and
    // the intended executable limit price. Fixed quantity remains a fallback.
    std::int64_t target_notional_microdollars = 0;
    std::int32_t maximum_entry_price_e4 = 8'000;
    std::uint8_t require_signal_valid = 1;
    std::uint8_t require_full_visible_depth = 1;
    std::uint8_t one_entry_per_market = 1;
    std::uint32_t required_slow_context_mask = 0;
    std::uint8_t probability_ev_enabled = 0;
    std::uint8_t require_pm_book_pre_signal = 0;
    std::array<std::uint8_t, 3> reserved{};
};

struct NativeCryptoInstrumentContext {
    std::uint64_t instrument_handle = 0;
    std::int64_t min_order_microunits = 0;
    std::uint8_t instrument_is_yes = 0;
    std::array<std::uint8_t, 7> reserved{};
};

struct NativeCryptoMarketContext {
    std::uint64_t market_handle = 0;
    std::uint64_t event_handle = 0;
    std::int64_t close_monotonic_ns = 0;
    NativeCryptoInstrumentContext yes{};
    NativeCryptoInstrumentContext no{};
    std::uint8_t accepting_orders = 0;
    std::uint8_t closed = 0;
    std::uint8_t contract_verified = 0;
    std::uint8_t settlement_reference_valid = 0;
    std::array<std::uint8_t, 4> reserved{};
};

struct NativeCryptoDecisionInput {
    external_fair::ExternalCancelSignalSnapshot signal{};
    NativeCryptoMarketContext market{};
    BookHotSnapshot yes_book{};
    BookHotSnapshot no_book{};
    std::int64_t now_monotonic_ns = 0;
    std::uint64_t model_version = 1;
    std::uint64_t policy_version = 1;
    SlowContextCut slow_context{};
    SettlementProbabilityForecast probability{};
    ProbabilityEvPolicy risk_sizing{};
    double taker_fee_rate = std::numeric_limits<double>::quiet_NaN();
    double taker_fee_exponent = std::numeric_limits<double>::quiet_NaN();
    double execution_reserve_per_share = std::numeric_limits<double>::quiet_NaN();
};

struct NativeCryptoDecisionResult {
    StrategyIntent intent{};
    ProbabilityEvDecision economics{};
    ExecutionAdmissionResult admission{};
    NativeCryptoDecisionReason reason = NativeCryptoDecisionReason::InvalidSignal;
    std::uint64_t signal_version = 0;
    std::uint64_t selected_instrument_handle = 0;
    std::int64_t signal_age_ns = 0;
    std::int64_t tte_ns = 0;
    std::int64_t decision_compute_ns = 0;
    std::uint8_t selected_yes = 0;
    std::uint8_t accepted = 0;
    std::array<std::uint8_t, 6> reserved{};
};

class NativeCryptoDecisionLane final {
public:
    explicit NativeCryptoDecisionLane(NativeCryptoDecisionPolicy policy) noexcept;

    // Build an already-causal taker candidate without reserving capital. The
    // unified settlement owner performs portfolio/risk/capital/OMS admission.
    [[nodiscard]] NativeCryptoDecisionResult construct_candidate(
        const NativeCryptoDecisionInput& input) noexcept;
    [[nodiscard]] NativeCryptoDecisionResult evaluate(
        const NativeCryptoDecisionInput& input,
        SleeveCapitalAccount& capital) noexcept;
    // A causal candidate is only a proposal. Consume its signal after the sole
    // execution owner has accepted new risk; arbitration/rejection must not
    // destroy a still-fresh opportunity.
    void commit_signal(std::uint64_t market_handle,
                       std::uint64_t signal_version) noexcept;
    void mark_market_traded(std::uint64_t market_handle) noexcept;
    void reset_market(std::uint64_t market_handle) noexcept;

private:
    [[nodiscard]] bool seen_signal(std::uint64_t market_handle,
                                   std::uint64_t signal_version) const noexcept;
    [[nodiscard]] bool market_traded(std::uint64_t market_handle) const noexcept;

    NativeCryptoDecisionPolicy policy_{};
    std::array<std::uint64_t, kMaxCapitalMarkets> last_signal_version_{};
    std::array<std::uint8_t, kMaxCapitalMarkets> traded_market_{};
    std::uint64_t next_intent_id_ = 1;
};
static_assert(std::is_trivially_copyable_v<NativeCryptoDecisionPolicy>);
static_assert(std::is_trivially_copyable_v<NativeCryptoInstrumentContext>);
static_assert(std::is_trivially_copyable_v<NativeCryptoMarketContext>);
static_assert(std::is_trivially_copyable_v<NativeCryptoDecisionInput>);
static_assert(std::is_trivially_copyable_v<NativeCryptoDecisionResult>);

} // namespace pm::v7
