#include "pm/v7_native_settlement_oms_endpoint.hpp"
#include "pm/v7_native_paper_execution.hpp"
#include "pm/v7_native_runtime_evidence.hpp"
#include "pm/fast_ws.hpp"
#include "pm/v7_coinbase_l2_observer.hpp"
#include "pm/v7_crypto_decision_lane.hpp"
#include "pm/v7_probability_model.hpp"
#include "pm/v7_pure_arb_lane.hpp"
#include "pm/v7_latency_trace.hpp"
#include "pm/v7_external_ingress.hpp"
#include "pm/v7_external_ws.hpp"
#include "pm/v7_ingress_wakeup.hpp"
#include "pm/v7_market_ws.hpp"
#include "pm/v7_maker_lane.hpp"
#include "pm/v7_native_settlement_authority.hpp"
#include "pm/v7_native_maker_context.hpp"
#include "pm/v7_spsc.hpp"

#include <boost/json.hpp>

#include <algorithm>
#include <array>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstdint>
#include <iostream>
#include <limits>
#include <memory>
#include <span>
#include <stop_token>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

namespace json = boost::json;
using namespace pm::v7;
using namespace pm::v7::external_fair;
using namespace std::chrono_literals;

namespace {
constexpr std::size_t kPmQueueCapacity = 8192;
constexpr std::size_t kPmFrameEvents = 1024;
static_assert(std::atomic<bool>::is_always_lock_free);
std::atomic<bool> shutdown_requested{false};
void request_shutdown(int) noexcept {
    shutdown_requested.store(true, std::memory_order_relaxed);
}

std::int64_t monotonic_now_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}
std::int64_t wall_now_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}

template <class T>
T bounded_integer(std::string_view text, T lo, T hi) {
    T value{};
    const auto result = std::from_chars(text.data(), text.data() + text.size(), value);
    if (result.ec != std::errc{} || result.ptr != text.data() + text.size()
        || value < lo || value > hi) throw std::invalid_argument("bounded integer required");
    return value;
}

double bounded_double(std::string_view text, double lo, double hi) {
    double value{};
    const auto result = std::from_chars(text.data(), text.data() + text.size(), value);
    if (result.ec != std::errc{} || result.ptr != text.data() + text.size()
        || !std::isfinite(value) || value < lo || value > hi) {
        throw std::invalid_argument("bounded floating point required");
    }
    return value;
}

bool exact_sha(std::string_view value) noexcept {
    if (value.size() != 40) return false;
    for (char ch : value) {
        if (!((ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f'))) return false;
    }
    return true;
}

struct Options {
    std::string asset = "BTC";
    std::string horizon = "M5";
    std::string binance_symbol = "BTCUSDT";
    std::string coinbase_symbol = "BTC-USD";
    std::string bybit_symbol = "NONE";
    std::string confirmation_venue = "COINBASE";
    std::string yes_token;
    std::string no_token;
    std::string run_root;
    std::string model_sha;
    std::string probability_model;
    std::string slow_context;
    std::int64_t probability_evaluation_end_wall_ns = 0;
    std::string run_id;
    std::string server_id;
    std::string market_id;
    std::string event_id;
    std::string fee_source;
    std::string pm_ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/market";
    std::int64_t market_start_wall_ns = 0;
    std::int64_t close_wall_ns = 0;
    std::int32_t tick_size_e4 = 100;
    std::int64_t min_order_microunits = 5'000'000;
    std::int64_t target_quantity_microunits = 20'000'000;
    std::int64_t target_notional_microdollars = 0;
    std::int32_t maximum_entry_price_e4 = 10'000;
    double minimum_absolute_binance_return_bp = 0.30;
    double minimum_absolute_confirmation_return_bp = 0.0;
    std::int64_t maximum_signal_age_ns = 5'000'000'000LL;
    std::int64_t minimum_tte_ns = 5'000'000'000LL;
    std::int64_t maximum_tte_ns = 120'000'000'000LL;
    std::int64_t maker_share_cap_microunits = 5'000'000;
    std::string risk_policy_sha256;
    CapitalLimits capital_limits{};
    double taker_fee_rate = 0.0;
    double taker_fee_exponent = 1.0;
    double pure_arb_reserve_per_share = 0.0005;
    std::int64_t pure_arb_max_leg_skew_ns = 100'000'000LL;
    bool pure_arb_native_shadow = false;
    int duration_seconds = 0;
    std::int64_t paper_venue_delay_ns = -1;
    std::int64_t paper_assumed_transport_delay_ns = 250'000'000LL;
    std::string paper_terms_sha256;
    std::string signal_policy_sha256;
    std::string latency_trace_bin;
    bool strict_signal_policy = false;
    bool validate_only = false;
    bool observation_only = false;
    bool capture_native_decisions = false;
    bool capture_native_observations = false;
    bool capture_execution_windows = false;
    std::int64_t execution_window_ns = 2'000'000'000LL;
};

Options parse_options(int argc, char** argv) {
    Options out;
    for (int i = 1; i < argc; ++i) {
        const std::string_view arg = argv[i];
        auto next = [&]() -> std::string_view {
            if (++i >= argc) throw std::invalid_argument("missing option value");
            return argv[i];
        };
        if (arg == "--asset") out.asset = next();
        else if (arg == "--horizon") out.horizon = next();
        else if (arg == "--binance-symbol") out.binance_symbol = next();
        else if (arg == "--coinbase-symbol") out.coinbase_symbol = next();
        else if (arg == "--bybit-symbol") out.bybit_symbol = next();
        else if (arg == "--confirmation-venue") out.confirmation_venue = next();
        else if (arg == "--yes-token") out.yes_token = next();
        else if (arg == "--no-token") out.no_token = next();
        else if (arg == "--run-root") out.run_root = next();
        else if (arg == "--model-sha") out.model_sha = next();
        else if (arg == "--probability-model") out.probability_model = next();
        else if (arg == "--slow-context") out.slow_context = next();
        else if (arg == "--probability-evaluation-end-wall-ns")
            out.probability_evaluation_end_wall_ns = bounded_integer<std::int64_t>(
                next(), 1, std::numeric_limits<std::int64_t>::max());
        else if (arg == "--run-id") out.run_id = next();
        else if (arg == "--server-id") out.server_id = next();
        else if (arg == "--market-id") out.market_id = next();
        else if (arg == "--event-id") out.event_id = next();
        else if (arg == "--fee-source") out.fee_source = next();
        else if (arg == "--pm-ws-url") out.pm_ws_url = next();
        else if (arg == "--market-start-wall-ns") out.market_start_wall_ns = bounded_integer<std::int64_t>(next(), 1, std::numeric_limits<std::int64_t>::max());
        else if (arg == "--close-wall-ns") out.close_wall_ns = bounded_integer<std::int64_t>(next(), 1, std::numeric_limits<std::int64_t>::max());
        else if (arg == "--tick-size-e4") out.tick_size_e4 = bounded_integer<std::int32_t>(next(), 1, 5000);
        else if (arg == "--min-order-microunits") out.min_order_microunits = bounded_integer<std::int64_t>(next(), 1, 1'000'000'000);
        else if (arg == "--target-quantity-microunits") out.target_quantity_microunits = bounded_integer<std::int64_t>(next(), 1, 1'000'000'000);
        else if (arg == "--target-notional-microdollars") out.target_notional_microdollars = bounded_integer<std::int64_t>(next(), 1, 333'333'333);
        else if (arg == "--maximum-entry-price-e4") out.maximum_entry_price_e4 = bounded_integer<std::int32_t>(next(), 1, 9'999);
        else if (arg == "--minimum-absolute-binance-return-bp") out.minimum_absolute_binance_return_bp = bounded_double(next(), 0.000001, 1000.0);
        else if (arg == "--minimum-absolute-confirmation-return-bp") out.minimum_absolute_confirmation_return_bp = bounded_double(next(), 0.0, 1000.0);
        else if (arg == "--maximum-signal-age-ns") out.maximum_signal_age_ns = bounded_integer<std::int64_t>(next(), 1, 5'000'000'000LL);
        else if (arg == "--minimum-tte-ns") out.minimum_tte_ns = bounded_integer<std::int64_t>(next(), 1, 86'400'000'000'000LL);
        else if (arg == "--maximum-tte-ns") out.maximum_tte_ns = bounded_integer<std::int64_t>(next(), 1, 86'400'000'000'000LL);
        else if (arg == "--maker-share-cap-microunits") out.maker_share_cap_microunits = bounded_integer<std::int64_t>(next(), 1, 5'000'000);
        else if (arg == "--risk-policy-sha256") out.risk_policy_sha256 = next();
        else if (arg == "--sleeve-budget-microdollars") out.capital_limits.sleeve_budget_microdollars = bounded_integer<std::int64_t>(next(), 1, 10'000'000'000LL);
        else if (arg == "--max-total-exposure-microdollars") out.capital_limits.max_total_exposure_microdollars = bounded_integer<std::int64_t>(next(), 1, 10'000'000'000LL);
        else if (arg == "--max-market-exposure-microdollars") out.capital_limits.max_market_exposure_microdollars = bounded_integer<std::int64_t>(next(), 1, 333'333'333);
        else if (arg == "--max-single-order-microdollars") out.capital_limits.max_single_order_microdollars = bounded_integer<std::int64_t>(next(), 1, 333'333'333);
        else if (arg == "--taker-fee-rate") out.taker_fee_rate = bounded_double(next(), 0.0, 1.0);
        else if (arg == "--taker-fee-exponent") out.taker_fee_exponent = bounded_double(next(), 0.0, 10.0);
        else if (arg == "--pure-arb-reserve-per-share") out.pure_arb_reserve_per_share = bounded_double(next(), 0.0, 0.25);
        else if (arg == "--pure-arb-max-leg-skew-ns") out.pure_arb_max_leg_skew_ns = bounded_integer<std::int64_t>(next(), 1'000, 5'000'000'000LL);
        else if (arg == "--pure-arb-native-shadow") out.pure_arb_native_shadow = true;
        else if (arg == "--duration-seconds") out.duration_seconds = bounded_integer<int>(next(), 0, 86'400);
        else if (arg == "--paper-venue-delay-ns") out.paper_venue_delay_ns = bounded_integer<std::int64_t>(next(), -1, 5'000'000'000LL);
        else if (arg == "--paper-assumed-transport-delay-ns") out.paper_assumed_transport_delay_ns = bounded_integer<std::int64_t>(next(), 1, 5'000'000'000LL);
        else if (arg == "--paper-terms-sha256") out.paper_terms_sha256 = next();
        else if (arg == "--signal-policy-sha256") out.signal_policy_sha256 = next();
        else if (arg == "--latency-trace-bin") out.latency_trace_bin = next();
        else if (arg == "--strict-signal-policy") out.strict_signal_policy = true;
        else if (arg == "--validate-only") out.validate_only = true;
        else if (arg == "--observation-only") {
            out.observation_only = true;
            out.capture_native_decisions = true;
        }
        else if (arg == "--capture-native-decisions") out.capture_native_decisions = true;
        else if (arg == "--capture-native-observations") {
            out.capture_native_observations = true;
            out.capture_native_decisions = true;
        }
        else if (arg == "--capture-execution-windows") {
            out.capture_execution_windows = true;
            out.capture_native_decisions = true;
        }
        else if (arg == "--execution-window-ns")
            out.execution_window_ns = bounded_integer<std::int64_t>(
                next(), 1'000'000LL, 10'000'000'000LL);
        else throw std::invalid_argument("unknown option");
    }
    return out;
}

struct PmQueuedEvent {
    MarketWsEvent event{};
    std::uint64_t connection_epoch = 0;
};

inline constexpr std::array<std::uint32_t, 18> kRepricingHorizonsMs{
    5, 10, 25, 50, 100, 250, 500, 750, 1000,
    1250, 1500, 1750, 2000, 3000, 4000, 5000, 7500, 10000
};
struct RepricingWindow {
    std::uint64_t signal_version = 0;
    std::uint64_t instrument_handle = 0;
    std::int64_t trigger_ns = 0;
    std::int64_t decision_ns = 0;
    std::array<std::int64_t, kRepricingHorizonsMs.size()> target_ns{};
    std::uint32_t emitted_mask = 0;
    // Signal direction is provenance. instrument_handle is the economically
    // selected token and owns the executable depth labels.
    std::int8_t direction = 0;
    std::uint8_t active = 0;
    std::uint8_t continuity_valid = 0;
};


json::object latency_distribution(std::vector<std::int64_t> values) {
    if (values.empty()) return {{"count", 0}, {"p50_ns", nullptr}, {"p95_ns", nullptr}, {"p99_ns", nullptr}, {"p999_ns", nullptr}, {"max_ns", nullptr}};
    std::sort(values.begin(), values.end());
    const auto q = [&](double probability) {
        const auto index = static_cast<std::size_t>(std::ceil(probability * values.size())) - 1;
        return values[std::min(index, values.size() - 1)];
    };
    return {{"count", values.size()}, {"p50_ns", q(.50)}, {"p95_ns", q(.95)}, {"p99_ns", q(.99)}, {"p999_ns", q(.999)}, {"max_ns", values.back()}};
}

json::object reason_json(const std::array<std::uint64_t, 32>& counts) {
    json::object out;
    for (std::size_t i = 0; i < counts.size(); ++i) if (counts[i] != 0) out[std::to_string(i)] = counts[i];
    return out;
}
} // namespace

int main(int argc, char** argv) {
    try {
        const auto options = parse_options(argc, argv);
        if ((options.target_notional_microdollars <= 0
                && options.target_quantity_microunits < options.min_order_microunits)
            || options.minimum_tte_ns <= 0
            || options.maximum_tte_ns < options.minimum_tte_ns) {
            throw std::invalid_argument("invalid PAPER sizing or tte policy");
        }
        if (options.paper_venue_delay_ns >= 0 && (options.paper_terms_sha256.size() != 64
            || !std::all_of(options.paper_terms_sha256.begin(), options.paper_terms_sha256.end(),
                [](char c) { return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'); })))
            throw std::invalid_argument("market terms hash required for delayed PAPER matching");
        if (options.strict_signal_policy
            && (options.signal_policy_sha256.size() != 64
                || !std::all_of(options.signal_policy_sha256.begin(), options.signal_policy_sha256.end(),
                    [](char c) { return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'); })))
            throw std::invalid_argument("strict signal policy hash required");
        const auto probability_model = options.probability_model.empty() ? NativeProbabilityModel{}
            : NativeProbabilityModel::load(options.probability_model, options.model_sha);
        if (probability_model.loaded && options.probability_evaluation_end_wall_ns <= 0)
            throw std::invalid_argument("probability model requires fixed evaluation end");
        if (!probability_model.loaded && options.probability_evaluation_end_wall_ns != 0)
            throw std::invalid_argument("probability evaluation end without model");
        if (options.validate_only) {
            std::cout << "native crypto settlement candidate configuration PASS\n";
            return 0;
        }
        if (options.asset.empty() || options.horizon.empty() || options.binance_symbol.empty()
            || options.yes_token.empty() || options.no_token.empty()
            || options.yes_token == options.no_token || options.run_root.empty()
            || !exact_sha(options.model_sha) || options.run_id.empty()
            || options.server_id.empty() || options.market_id.empty()
            || options.event_id.empty() || options.fee_source.empty()
            || options.market_start_wall_ns <= 0
            || options.market_start_wall_ns >= options.close_wall_ns
            || options.close_wall_ns <= wall_now_ns()
            || (options.pure_arb_native_shadow && options.latency_trace_bin.empty())) {
            throw std::invalid_argument("live PAPER runtime identity required");
        }

        if (options.binance_symbol != options.asset + "USDT"
            || (!options.coinbase_symbol.empty() && options.coinbase_symbol != "NONE"
                && options.coinbase_symbol != options.asset + "-USD")
            || (!options.bybit_symbol.empty() && options.bybit_symbol != "NONE"
                && options.bybit_symbol != options.asset + "USDT"))
            throw std::invalid_argument("external symbol does not match crypto context");
        if (options.confirmation_venue != "COINBASE" && options.confirmation_venue != "BYBIT")
            throw std::invalid_argument("confirmation venue must be COINBASE or BYBIT");
        if (options.strict_signal_policy
            && ((options.confirmation_venue == "COINBASE"
                    && (options.coinbase_symbol.empty() || options.coinbase_symbol == "NONE"))
                || (options.confirmation_venue == "BYBIT"
                    && (options.bybit_symbol.empty() || options.bybit_symbol == "NONE"))))
            throw std::invalid_argument("strict signal policy confirmation venue has no symbol");

        constexpr std::uint64_t kAsset = 1, kMarket = 1, kEvent = 1, kYes = 1, kNo = 2;
        IngressWakeup wakeup;
        ExternalVenueIngress binance_ingress(VenueId::BinanceSpot, kAsset, nullptr, &wakeup);
        ExternalVenueIngress coinbase_ingress(VenueId::CoinbaseSpot, kAsset, nullptr, &wakeup);
        ExternalVenueIngress bybit_ingress(VenueId::BybitSpot, kAsset, nullptr, &wakeup);
        auto binance_spec = crypto_connection_spec(
            VenueId::BinanceSpot, kAsset, options.binance_symbol);
        ExternalVenueWsClient binance(binance_spec, &binance_ingress);
        std::unique_ptr<CoinbaseL2FrameObserver> coinbase_l2;
        std::unique_ptr<ExternalVenueWsClient> coinbase;
        std::unique_ptr<ExternalVenueWsClient> bybit;
        if (!options.coinbase_symbol.empty() && options.coinbase_symbol != "NONE") {
            auto coinbase_spec = crypto_connection_spec(
                VenueId::CoinbaseSpot, kAsset, options.coinbase_symbol);
            coinbase_l2 = std::make_unique<CoinbaseL2FrameObserver>(
                coinbase_ingress, kAsset);
            coinbase = std::make_unique<ExternalVenueWsClient>(
                std::move(coinbase_spec), nullptr, coinbase_l2.get());
        }
        if (!options.bybit_symbol.empty() && options.bybit_symbol != "NONE") {
            auto bybit_spec = crypto_connection_spec(
                VenueId::BybitSpot, kAsset, options.bybit_symbol);
            // The generic decoder owns a bounded top-of-book confirmation lane.
            // Do not reuse the 50-level stateful research observer in the hot path.
            bybit_spec.subscription_json =
                "{\"op\":\"subscribe\",\"args\":[\"orderbook.1."
                + options.bybit_symbol + "\",\"publicTrade." + options.bybit_symbol + "\"]}";
            bybit = std::make_unique<ExternalVenueWsClient>(
                std::move(bybit_spec), &bybit_ingress);
        }

        std::vector<TokenBinding> bindings{
            {options.yes_token, kMarket, kEvent, kYes, options.tick_size_e4},
            {options.no_token, kMarket, kEvent, kNo, options.tick_size_e4},
        };
        MarketWsShard pm_decoder(std::move(bindings));
        SpscRing<PmQueuedEvent, kPmQueueCapacity> pm_queue;
        std::atomic<std::uint64_t> pm_drops{0}, pm_faults{0};
        std::atomic<std::uint64_t> pm_epoch{1};

        // Allocate bounded parser output once, before any feed thread starts.
        // macOS worker stacks are about 512 KiB; a 1024-event BookHotSnapshot
        // scratch array on that stack crashes before the first frame is parsed.
        auto pm_decoded_scratch = std::make_unique<std::array<MarketWsEvent, kPmFrameEvents>>();
        pm::fast::MarketWebSocketFeed pm_feed(
            options.pm_ws_url, {options.yes_token, options.no_token}, 2,
            [&](std::string_view payload, const pm::fast::FeedReceiveStamp& stamp, std::size_t) {
                auto& decoded = *pm_decoded_scratch;
                const auto result = pm_decoder.process_frame(payload, stamp, decoded);
                bool notified = false;
                if (result.invalid_frame || result.output_overflow || result.arena_exhausted || result.lineage_invalidated) {
                    pm_faults.fetch_add(1, std::memory_order_relaxed);
                }
                for (std::size_t i = 0; i < result.output_count; ++i) {
                    PmQueuedEvent queued;
                    queued.event = decoded[i];
                    queued.connection_epoch = pm_epoch.load(std::memory_order_acquire);
                    if (!pm_queue.try_push(queued)) {
                        pm_drops.fetch_add(1, std::memory_order_relaxed);
                        pm_faults.fetch_add(1, std::memory_order_relaxed);
                    } else notified = true;
                }
                if (notified || result.invalid_frame || result.output_overflow || result.arena_exhausted) wakeup.notify();
            },
            [&](std::size_t, std::string_view) {
                pm_decoder.invalidate_all_lineage();
                pm_epoch.fetch_add(1, std::memory_order_acq_rel);
                pm_faults.fetch_add(1, std::memory_order_relaxed);
                wakeup.notify();
            });

        ExternalStatePolicy external_policy;
        external_policy.external_cancel_enabled = 1;
        external_policy.external_cancel_shock_window_ns = 100'000'000LL;
        external_policy.external_cancel_grid_ns = 25'000'000LL;
        external_policy.external_cancel_cooldown_ns = 250'000'000LL;
        external_policy.external_cancel_warmup_ns = 300'000'000LL;
        external_policy.external_cancel_signal_ttl_ns = options.strict_signal_policy
            ? options.maximum_signal_age_ns : 100'000'000LL;
        external_policy.external_cancel_min_abs_return_bp =
            options.strict_signal_policy ? options.minimum_absolute_binance_return_bp : 0.30;
        external_policy.external_cancel_min_abs_confirmation_return_bp =
            options.strict_signal_policy ? options.minimum_absolute_confirmation_return_bp : 0.0;
        external_policy.external_cancel_confirmation_venue =
            options.confirmation_venue == "BYBIT" ? VenueId::BybitSpot : VenueId::CoinbaseSpot;
        ExternalAssetState external_state(kAsset);

        NativeCryptoDecisionPolicy decision_policy;
        // The experimental per-asset policy makes signal lifetime explicit.
        // Legacy callers retain their existing defaults unless they pass overrides.
        decision_policy.require_signal_valid = options.strict_signal_policy ? 1 : 0;
        decision_policy.maximum_signal_age_ns = options.maximum_signal_age_ns;
        decision_policy.minimum_absolute_binance_return_bp =
            options.minimum_absolute_binance_return_bp;
        decision_policy.require_pm_book_pre_signal = options.strict_signal_policy ? 1 : 0;
        decision_policy.minimum_tte_ns = options.minimum_tte_ns;
        decision_policy.maximum_tte_ns = options.maximum_tte_ns;
        decision_policy.target_quantity_microunits = options.target_quantity_microunits;
        decision_policy.target_notional_microdollars = options.target_notional_microdollars;
        decision_policy.maximum_entry_price_e4 = options.maximum_entry_price_e4;
        // Signal admission and probability/EV valuation are separate contracts.
        // A strict causal signal policy must never silently activate an absent
        // probability artifact.
        decision_policy.probability_ev_enabled = probability_model.loaded ? 1 : 0;
        NativeCryptoDecisionLane lane(decision_policy);
        CapitalLimits limits = options.capital_limits;
        if (!options.observation_only && (!limits.valid() || options.risk_policy_sha256.size() != 64
            || !std::all_of(options.risk_policy_sha256.begin(), options.risk_policy_sha256.end(),
                [](char c) { return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'); })))
            throw std::invalid_argument("validated canonical capital policy required");
        if (options.observation_only && !limits.valid()) {
            // No order is admissible in this mode; zero-risk probes need no funds.
            limits = {1'000'000, 1'000'000, 1'000'000, 1'000'000};
        }
        NativeSettlementAuthority authority(limits);
        NativeSettlementOmsEndpoint adapter_endpoint(authority);
        const auto paper_taker_delay_ns = options.paper_venue_delay_ns < 0 ? -1
            : options.paper_venue_delay_ns + options.paper_assumed_transport_delay_ns;
        NativePaperExecutionAdapter paper_execution(adapter_endpoint, 100'000'000LL,
            paper_taker_delay_ns, decision_policy.maximum_book_age_ns);
        NativeRuntimeEvidenceConfig evidence_config{};
        evidence_config.run_root = options.run_root;
        evidence_config.model_sha = options.model_sha;
        evidence_config.run_id = options.run_id;
        evidence_config.server_id = options.server_id;
        evidence_config.asset = options.asset;
        evidence_config.horizon = options.horizon;
        evidence_config.market_id = options.market_id;
        evidence_config.event_id = options.event_id;
        evidence_config.yes_token_id = options.yes_token;
        evidence_config.no_token_id = options.no_token;
        evidence_config.fee_source = options.fee_source;
        evidence_config.probability_artifact_sha256 = probability_model.artifact_sha256;
        evidence_config.probability_evaluation_end_wall_ns =
            options.probability_evaluation_end_wall_ns;
        evidence_config.yes_instrument_handle = kYes;
        evidence_config.no_instrument_handle = kNo;
        evidence_config.close_wall_ns = options.close_wall_ns;
        evidence_config.taker_fee_rate = options.taker_fee_rate;
        evidence_config.taker_fee_exponent = options.taker_fee_exponent;
        evidence_config.taker_maximum_entry_price_e4 = options.maximum_entry_price_e4;
        evidence_config.taker_only_fee = 1;
        evidence_config.paper_venue_delay_ns = options.paper_venue_delay_ns;
        evidence_config.paper_assumed_transport_delay_ns = options.paper_assumed_transport_delay_ns;
        evidence_config.paper_terms_sha256 = options.paper_terms_sha256;
        evidence_config.signal_policy_sha256 = options.signal_policy_sha256;
        evidence_config.observation_capture_mode = options.capture_native_observations
            ? "FULL" : options.capture_execution_windows ? "DECISION_WINDOWS"
            : options.capture_native_decisions ? "DECISIONS" : "NONE";
        evidence_config.minimum_order_microunits = options.min_order_microunits;
        evidence_config.risk_policy_sha256 = options.risk_policy_sha256;
        evidence_config.asset = options.asset;
        evidence_config.horizon = options.horizon;
        // Zero-authority shadow begins from an explicit flat canonical inventory
        // snapshot. Non-zero recovery inventory must come from the future native
        // recovery/reconciliation boundary; strategy lanes never synthesize it.
        if (!authority.sync_inventory(kMarket, kYes, 0, 0, 1)
            || !authority.sync_inventory(kMarket, kNo, 0, 0, 1)) {
            throw std::runtime_error("native inventory bootstrap failed");
        }
        maker::MakerInstrumentLane yes_maker(+1), no_maker(-1);
        maker::MakerModelSnapshot maker_model;
        if (!maker_model.valid()) throw std::runtime_error("invalid native maker model snapshot");
        evidence_config.maker_artifact_sha256 = maker_model.execution_artifact_sha256.data();
        evidence_config.maker_policy_sha256 = maker_model.exploration_policy_sha256.data();
        evidence_config.maker_execution_policy_hash = maker_model.execution_policy_hash.data();
        evidence_config.maker_execution_config_hash = maker_model.execution_config_hash.data();
        evidence_config.maker_execution_semantics = maker_model.execution_semantics_version.data();
        evidence_config.maker_valid_cells = std::count_if(maker_model.execution_cells.begin(),
            maker_model.execution_cells.end(), [](const auto& cell) { return cell.valid != 0; });
        auto evidence_owner = std::make_unique<NativeRuntimeEvidenceWriter>(evidence_config);
        auto& evidence_writer = *evidence_owner;
        std::unique_ptr<NativeLatencyTraceWriter> latency_trace_writer;
        if (!options.latency_trace_bin.empty()) {
            latency_trace_writer =
                std::make_unique<NativeLatencyTraceWriter>(options.latency_trace_bin);
            if (!latency_trace_writer->valid()) {
                throw std::runtime_error("native latency trace writer unavailable");
            }
        }
        maker::MakerLaneContext maker_context;
        maker_context.risk.max_quote_shares = options.maker_share_cap_microunits / 1'000'000.0;
        maker_context.risk.max_abs_residual_shares = options.maker_share_cap_microunits / 1'000'000.0;

        const auto start_mono = monotonic_now_ns();
        const auto start_wall = wall_now_ns();
        NativeCryptoMarketContext market;
        market.market_handle = kMarket;
        market.event_handle = kEvent;
        market.close_monotonic_ns = start_mono + (options.close_wall_ns - start_wall);
        market.yes = {kYes, options.min_order_microunits, 1, {}};
        market.no = {kNo, options.min_order_microunits, 0, {}};
        market.accepting_orders = 1;
        market.contract_verified = 1;
        market.settlement_reference_valid = 1;

        SlowContextCache slow_cache;
        SlowContextFeed slow_feed(options.slow_context,
            {options.model_sha, options.run_id, options.market_id, options.asset, options.horizon});
        SlowContextCut decision_slow_context{};
        std::uint64_t slow_context_updates = 0, external_protective_cancels = 0;
        std::uint64_t maker_blocked_by_fast_shock = 0;
        std::uint64_t last_protective_signal = 0;
        BookHotSnapshot yes_book{}, no_book{};
        std::uint64_t yes_book_epoch = 0, no_book_epoch = 0;
        std::uint64_t pure_arb_trace_sequence = 0;
        ExternalCancelSignalSnapshot current_signal{};
        SettlementProbabilityForecast decision_probability{};
        ProbabilityEvDecision decision_economics{};
        std::array<double, kProbabilityFeatures> decision_features{};
        std::uint64_t probability_input_instrument = 0;
        ExternalVenueEvent pending_binance{}, pending_coinbase{}, pending_bybit{};
        PmQueuedEvent pending_pm{};
        bool binance_ready = false, coinbase_ready = false, bybit_ready = false, pm_ready = false;
        std::vector<std::int64_t> accepted_signal_to_admission;
        std::vector<std::int64_t> accepted_signal_to_adapter;
        std::vector<std::int64_t> first_signal_to_decision;
        std::vector<std::int64_t> decision_compute;
        std::vector<std::int64_t> pure_arb_shadow_receive_to_decision;
        accepted_signal_to_admission.reserve(4096);
        accepted_signal_to_adapter.reserve(4096);
        first_signal_to_decision.reserve(4096);
        decision_compute.reserve(4096);
        pure_arb_shadow_receive_to_decision.reserve(4096);
        std::array<std::uint64_t, 32> reasons{};
        std::uint64_t evaluations = 0, accepted = 0, latency_overflow = 0;
        std::uint64_t taker_accepted = 0, maker_accepted = 0;
        std::uint64_t adapter_handoff_failures = 0;
        std::uint64_t maker_decisions = 0, maker_candidates = 0;
        std::uint64_t maker_cancel_intents = 0, maker_cancel_handoffs = 0;
        std::uint64_t maker_cancel_not_ready = 0, maker_duplicate_quotes = 0;
        std::uint64_t maker_replace_pending = 0;
        std::uint64_t maker_inadmissible_quantity = 0;
        std::uint64_t paper_trade_sequence = 0, paper_fill_events = 0;
        std::uint64_t paper_invalid_trades = 0, paper_submit_failures = 0;
        std::uint64_t paper_arrival_censored = 0, paper_arrival_observed_nonfills = 0;
        std::uint64_t arbitration_conflicts = 0, authority_rejections = 0;
        std::uint64_t inventory_rejections = 0, minimum_size_rejections = 0;
        std::uint64_t pure_arb_shadow_evaluations = 0;
        std::uint64_t pure_arb_shadow_buy_positive = 0;
        std::uint64_t pure_arb_shadow_sell_positive = 0;
        std::uint64_t pure_arb_shadow_buy_executable = 0;
        std::uint64_t pure_arb_shadow_sell_executable = 0;
        std::uint64_t pure_arb_shadow_stale_pair = 0;
        std::uint64_t pure_arb_shadow_latency_overflow = 0;
        double pure_arb_shadow_last_buy_edge = 0.0;
        double pure_arb_shadow_last_sell_edge = 0.0;
        double pure_arb_shadow_max_buy_edge = 0.0;
        double pure_arb_shadow_max_sell_edge = 0.0;
        double pure_arb_shadow_last_buy_shares = 0.0;
        double pure_arb_shadow_last_sell_shares = 0.0;
        std::int64_t pure_arb_shadow_last_receive_to_decision_ns = 0;
        std::int64_t pure_arb_shadow_max_receive_to_decision_ns = 0;
        std::uint64_t last_measured_signal_version = 0, last_observed_signal_version = 0;
        std::uint64_t last_execution_window_signal_version = 0;
        std::uint64_t last_execution_window_instrument = 0;
        std::uint8_t last_execution_window_reason = 0, last_execution_window_accepted = 0;
        std::int64_t execution_window_until_ns = 0;
        std::array<RepricingWindow, 64> repricing_windows{};
        std::uint64_t repricing_origins = 0, repricing_labels = 0;
        std::uint64_t repricing_censors = 0, repricing_window_overflow = 0;
        std::int64_t repricing_evidence_compute_ns = 0, repricing_evidence_max_ns = 0;
        std::uint8_t last_observed_reason = 0, last_observed_accepted = 0;
        std::int64_t last_control_observation_ns = 0;
        const auto observation = [&](const BookHotSnapshot& book, std::uint64_t instrument,
                                     std::uint8_t kind) noexcept {
            NativeObservation out{};
            out.instrument_handle = instrument; out.kind = kind;
            out.book_version = book.state_version; out.signal_version = current_signal.signal_version;
            out.connection_epoch = pm_epoch.load(std::memory_order_acquire);
            out.receive_ns = book.receive_monotonic_ns; out.exchange_ns = book.exchange_event_ns;
            out.observed_ns = monotonic_now_ns(); out.close_ns = market.close_monotonic_ns;
            out.trigger_ns = current_signal.trigger_receive_monotonic_ns;
            out.evaluated_grid_ns = current_signal.evaluated_grid_monotonic_ns;
            out.valid_until_ns = current_signal.valid_until_monotonic_ns;
            out.direction = current_signal.direction;
            out.signal_return_bp = current_signal.binance_return_100ms_bp;
            out.binance_return_100ms_bp = current_signal.binance_return_100ms_bp;
            out.coinbase_return_100ms_bp = current_signal.coinbase_return_100ms_bp;
            out.confirmation_return_100ms_bp = current_signal.confirmation_return_100ms_bp;
            out.confirmation_venue = current_signal.confirmation_venue;
            out.confirmed_non_opposing = current_signal.confirmed_non_opposing;
            out.signal_valid = current_signal.valid;
            out.valid = book.valid != 0 && book.lineage_continuous != 0;
            out.bid_e4 = book.best_bid_e4; out.ask_e4 = book.best_ask_e4; out.tick_e4 = book.tick_size_e4;
            out.bid_quantity = book.best_bid_microunits; out.ask_quantity = book.best_ask_microunits;
            for (std::size_t i = 0; i < 10; ++i) {
                if (i < book.bid_level_count && i < book.bid_levels.size()) {
                    out.bid_prices[i] = book.bid_levels[i].price_e4;
                    out.bid_quantities[i] = book.bid_levels[i].quantity_microunits;
                }
                if (i < book.ask_level_count && i < book.ask_levels.size()) {
                    out.ask_prices[i] = book.ask_levels[i].price_e4;
                    out.ask_quantities[i] = book.ask_levels[i].quantity_microunits;
                }
            }
            return out;
        };
        const auto decorate_pm_pair = [&](NativeObservation& out) noexcept {
            const bool yes_valid = yes_book.valid != 0 && yes_book.lineage_continuous != 0
                && yes_book.best_bid_e4 > 0 && yes_book.best_ask_e4 > yes_book.best_bid_e4
                && yes_book.best_ask_e4 < 10'000;
            const bool no_valid = no_book.valid != 0 && no_book.lineage_continuous != 0
                && no_book.best_bid_e4 > 0 && no_book.best_ask_e4 > no_book.best_bid_e4
                && no_book.best_ask_e4 < 10'000;
            out.repricing_pair_valid = yes_valid && no_valid ? 1 : 0;
            if (out.repricing_pair_valid != 0) {
                out.yes_bid_e4 = yes_book.best_bid_e4; out.yes_ask_e4 = yes_book.best_ask_e4;
                out.no_bid_e4 = no_book.best_bid_e4; out.no_ask_e4 = no_book.best_ask_e4;
                out.yes_bid_quantity = yes_book.best_bid_microunits;
                out.yes_ask_quantity = yes_book.best_ask_microunits;
                out.no_bid_quantity = no_book.best_bid_microunits;
                out.no_ask_quantity = no_book.best_ask_microunits;
            }
        };
        const auto start_repricing_window = [&](std::uint64_t signal_version,
                                                 std::int64_t trigger_ns,
                                                 std::int64_t decision_ns,
                                                 std::int8_t direction,
                                                 std::uint64_t instrument_handle) noexcept {
            for (auto& window : repricing_windows) {
                if (window.active != 0) continue;
                window = RepricingWindow{};
                window.signal_version = signal_version;
                window.instrument_handle = instrument_handle;
                window.trigger_ns = trigger_ns;
                window.decision_ns = decision_ns;
                window.direction = direction;
                window.active = 1;
                window.continuity_valid = 1;
                for (std::size_t i = 0; i < kRepricingHorizonsMs.size(); ++i) {
                    window.target_ns[i] = decision_ns
                        + static_cast<std::int64_t>(kRepricingHorizonsMs[i]) * 1'000'000LL;
                }
                ++repricing_origins;
                return;
            }
            ++repricing_window_overflow;
        };
        const auto emit_repricing_labels_before = [&](std::int64_t watermark_ns) noexcept {
            if (!options.capture_execution_windows || watermark_ns <= 0) return;
            for (auto& window : repricing_windows) {
                if (window.active == 0) continue;
                for (std::size_t i = 0; i < kRepricingHorizonsMs.size(); ++i) {
                    const auto mask = static_cast<std::uint32_t>(1U << i);
                    if ((window.emitted_mask & mask) != 0 || window.target_ns[i] >= watermark_ns) continue;
                    const auto evidence_started_ns = monotonic_now_ns();
                    const bool selected_up = window.instrument_handle == kYes;
                    if (!selected_up && window.instrument_handle != kNo) {
                        window.continuity_valid = 0;
                    }
                    auto point = observation(selected_up ? yes_book : no_book,
                                             selected_up ? kYes : kNo, 6);
                    point.signal_version = window.signal_version;
                    point.repricing_origin_signal_version = window.signal_version;
                    point.repricing_horizon_ms = kRepricingHorizonsMs[i];
                    point.decision_ns = window.decision_ns;
                    point.trigger_ns = window.trigger_ns;
                    point.direction = window.direction;
                    decorate_pm_pair(point);
                    if (window.continuity_valid == 0) point.repricing_pair_valid = 0;
                    if (!evidence_writer.publish_observation(point)) ++adapter_handoff_failures;
                    if (point.repricing_pair_valid != 0) ++repricing_labels;
                    else ++repricing_censors;
                    window.emitted_mask = static_cast<std::uint32_t>(window.emitted_mask | mask);
                    const auto evidence_elapsed_ns = monotonic_now_ns() - evidence_started_ns;
                    repricing_evidence_compute_ns += evidence_elapsed_ns;
                    repricing_evidence_max_ns = std::max(repricing_evidence_max_ns, evidence_elapsed_ns);
                }
                if (window.emitted_mask == ((1U << kRepricingHorizonsMs.size()) - 1U)) window.active = 0;
            }
        };
        const auto publish_order = [&](const NativeOrderCommand& command,
                                       ExecutionPolicyId policy,
                                       std::int64_t exchange_event_ns,
                                       std::int64_t receive_monotonic_ns) noexcept {
            NativeEvidenceEvent evidence{};
            evidence.kind = NativeEvidenceKind::OrderSubmitted;
            if (policy == ExecutionPolicyId::AggressiveTaker) {
                evidence.slow_context = decision_slow_context;
                evidence.probability = decision_probability;
                evidence.economics = decision_economics;
                evidence.probability_features = decision_features;
                evidence.probability_input_instrument = probability_input_instrument;
            }
            evidence.command = command;
            evidence.strategy_id = policy == ExecutionPolicyId::AggressiveTaker
                ? StrategyId::CryptoInformedTaker : StrategyId::ProfessionalMaker;
            evidence.policy = policy;
            evidence.causal_exchange_event_ns = exchange_event_ns;
            evidence.causal_receive_monotonic_ns = receive_monotonic_ns;
            evidence.recorded_monotonic_ns = monotonic_now_ns();
            return evidence_writer.publish(evidence);
        };
        const auto publish_state = [&](const NativeOrderCommand& command,
                                       ExecutionPolicyId policy,
                                       OrderState state,
                                       NativePaperReason reason = NativePaperReason::Accepted,
                                       bool censored = false) noexcept {
            NativeEvidenceEvent evidence{};
            evidence.kind = NativeEvidenceKind::OrderState;
            evidence.paper_reason = reason;
            evidence.paper_censored = censored;
            evidence.command = command;
            evidence.strategy_id = policy == ExecutionPolicyId::AggressiveTaker
                ? StrategyId::CryptoInformedTaker : StrategyId::ProfessionalMaker;
            evidence.policy = policy;
            evidence.order_state = state;
            evidence.recorded_monotonic_ns = monotonic_now_ns();
            return evidence_writer.publish(evidence);
        };
        const auto publish_fill = [&](const NativePaperFillRecord& fill,
                                      ExecutionPolicyId policy) noexcept {
            NativeEvidenceEvent evidence{};
            evidence.kind = NativeEvidenceKind::Fill;
            evidence.command = fill.command;
            evidence.fill = fill;
            evidence.strategy_id = policy == ExecutionPolicyId::AggressiveTaker
                ? StrategyId::CryptoInformedTaker : StrategyId::ProfessionalMaker;
            evidence.policy = policy;
            evidence.order_state = fill.order_state;
            evidence.recorded_monotonic_ns = monotonic_now_ns();
            return evidence_writer.publish(evidence);
        };

        // Risk-off has no dependency on slow context, fair inference or a new PM tick.
        const auto protective_cancel = [&](std::uint64_t instrument, Side side, std::int64_t now) {
            const auto cancel = authority.cancel_maker_quote(instrument, side, now);
            if (cancel.accepted != 0) {
                ++external_protective_cancels;
                if (!paper_execution.request_cancel(cancel.command, now)) ++adapter_handoff_failures;
            }
        };

        const auto consume_arrivals = [&](std::uint64_t instrument,
                                          const BookHotSnapshot& previous_book,
                                          std::int64_t watermark) {
            const auto batch = paper_execution.advance_arrivals(instrument, previous_book, watermark);
            adapter_handoff_failures += batch.invalid;
            for (std::size_t i = 0; i < batch.count; ++i) {
                const auto& item = batch.records[i];
                const auto& result = item.result;
                if (result.filled_microunits > 0) {
                    ++paper_fill_events;
                    lane.mark_market_traded(kMarket);
                    if (!publish_fill(result.fill, ExecutionPolicyId::AggressiveTaker)) ++adapter_handoff_failures;
                } else if (result.censored) ++paper_arrival_censored;
                else ++paper_arrival_observed_nonfills;
                if (!publish_state(item.command, ExecutionPolicyId::AggressiveTaker,
                                   result.final_state, result.reason, result.censored != 0)) ++adapter_handoff_failures;
            }
        };

#if defined(__APPLE__)
        std::atomic<bool> stopping{false};
        ExternalStopToken stop_token(stopping);
#else
        std::stop_source stopping;
        auto stop_token = stopping.get_token();
#endif
        slow_feed.start();
        std::thread binance_thread([&] { binance.run(stop_token); });
        std::thread coinbase_thread;
        std::thread bybit_thread;
        if (coinbase) {
            coinbase_thread = std::thread([&] { coinbase->run(stop_token); });
        }
        if (bybit) {
            bybit_thread = std::thread([&] { bybit->run(stop_token); });
        }
        pm_feed.start();

        const std::int64_t requested_deadline = options.duration_seconds > 0
            ? start_mono + static_cast<std::int64_t>(options.duration_seconds)
                * static_cast<std::int64_t>(1'000'000'000)
            : market.close_monotonic_ns;
        const std::int64_t deadline = std::min<std::int64_t>(
            requested_deadline, market.close_monotonic_ns);
        const auto refill_binance = [&] {
            if (!binance_ready) {
                binance_ready = binance_ingress.drain_events(
                    std::span<ExternalVenueEvent>(&pending_binance, 1), 1) == 1;
            }
        };
        const auto refill_coinbase = [&] {
            if (!coinbase_ready) {
                coinbase_ready = coinbase_ingress.drain_events(
                    std::span<ExternalVenueEvent>(&pending_coinbase, 1), 1) == 1;
            }
        };
        const auto refill_bybit = [&] {
            if (!bybit_ready) {
                bybit_ready = bybit_ingress.drain_events(
                    std::span<ExternalVenueEvent>(&pending_bybit, 1), 1) == 1;
            }
        };
        const auto refill_pm = [&] {
            while (!pm_ready && pm_queue.try_pop(pending_pm)) {
                const auto epoch = pm_epoch.load(std::memory_order_acquire);
                if (pending_pm.connection_epoch == epoch) pm_ready = true;
            }
        };
        // SIGTERM follows the same bounded PAPER cancellation and evidence
        // drain as a natural rollover, so deployments seal capture prefixes.
        std::signal(SIGTERM, request_shutdown);
        std::signal(SIGINT, request_shutdown);
        while (!shutdown_requested.load(std::memory_order_relaxed) && monotonic_now_ns() < deadline) {
            if (pm_faults.exchange(0, std::memory_order_acq_rel) != 0) {
                const auto fault_now = monotonic_now_ns();
                for (const auto instrument : {kYes, kNo})
                    for (const auto side : {Side::Buy, Side::Sell})
                        protective_cancel(instrument, side, fault_now);
                paper_execution.invalidate_arrivals();
                for (auto& window : repricing_windows) if (window.active != 0) window.continuity_valid = 0;
                const bool repricing_active = std::any_of(repricing_windows.begin(), repricing_windows.end(),
                    [](const auto& window) { return window.active != 0; });
                yes_book.valid = 0; yes_book.lineage_continuous = 0;
                no_book.valid = 0; no_book.lineage_continuous = 0;
                yes_book_epoch = no_book_epoch = 0;
                if (options.capture_native_observations || (options.capture_execution_windows && repricing_active)) {
                    if (!evidence_writer.publish_observation(observation(yes_book, kYes, 5))) ++adapter_handoff_failures;
                    if (!evidence_writer.publish_observation(observation(no_book, kNo, 5))) ++adapter_handoff_failures;
                }
                if (pm_ready && pending_pm.connection_epoch != pm_epoch.load(std::memory_order_acquire)) {
                    pm_ready = false;
                }
            }
            slow_context_updates += slow_feed.consume(slow_cache, monotonic_now_ns());
            refill_binance();
            refill_coinbase();
            refill_bybit();
            refill_pm();
            if (!binance_ready && !coinbase_ready && !bybit_ready && !pm_ready) {
                (void)wakeup.wait_for(2ms);
                continue;
            }
            std::int64_t receive_ns = std::numeric_limits<std::int64_t>::max();
            if (binance_ready) receive_ns = std::min(receive_ns, pending_binance.local_receive_monotonic_ns);
            if (coinbase_ready) receive_ns = std::min(receive_ns, pending_coinbase.local_receive_monotonic_ns);
            if (bybit_ready) receive_ns = std::min(receive_ns, pending_bybit.local_receive_monotonic_ns);
            if (pm_ready) receive_ns = std::min(receive_ns, pending_pm.event.receive_monotonic_ns);
            if (receive_ns <= 0 || receive_ns == std::numeric_limits<std::int64_t>::max()) {
                if (latency_overflow < 3) {
                    std::cerr << "invalid_ingress_clock binance_ready=" << binance_ready
                              << " binance_ns=" << pending_binance.local_receive_monotonic_ns
                              << " coinbase_ready=" << coinbase_ready
                              << " coinbase_ns=" << pending_coinbase.local_receive_monotonic_ns
                              << " bybit_ready=" << bybit_ready
                              << " bybit_ns=" << pending_bybit.local_receive_monotonic_ns
                              << " pm_ready=" << pm_ready
                              << " pm_ns=" << pending_pm.event.receive_monotonic_ns << '\n';
                }
                ++latency_overflow;
                binance_ready = coinbase_ready = bybit_ready = pm_ready = false;
                continue;
            }
            emit_repricing_labels_before(receive_ns);
            const auto paper_advance = paper_execution.advance_time(receive_ns);
            if (paper_advance.invalid != 0) {
                ++adapter_handoff_failures;
                break;
            }
            for (std::size_t i = 0; i < paper_advance.cancellation_count; ++i) {
                if (!publish_state(paper_advance.cancellations[i].command,
                                   ExecutionPolicyId::PassiveMaker,
                                   OrderState::Cancelled)) {
                    ++adapter_handoff_failures;
                    break;
                }
            }
            if (!evidence_writer.healthy()) {
                ++adapter_handoff_failures;
                break;
            }
            bool has_external = (binance_ready && pending_binance.local_receive_monotonic_ns == receive_ns)
                || (coinbase_ready && pending_coinbase.local_receive_monotonic_ns == receive_ns)
                || (bybit_ready && pending_bybit.local_receive_monotonic_ns == receive_ns);
            if (has_external && receive_ns > 1) {
                current_signal = external_state.advance_external_cancel_signal(receive_ns - 1, external_policy);
            }
            std::array<ExecutionPlan, 8> alpha_candidates{};
            std::array<std::int64_t, 8> candidate_trigger_ns{};
            std::array<std::uint8_t, 8> candidate_is_taker{};
            std::size_t alpha_candidate_count = 0;
            const auto append_candidate = [&](const ExecutionPlan& plan,
                                              std::int64_t trigger_ns,
                                              bool is_taker) noexcept {
                if (alpha_candidate_count >= alpha_candidates.size()) {
                    ++latency_overflow;
                    return;
                }
                alpha_candidates[alpha_candidate_count] = plan;
                candidate_trigger_ns[alpha_candidate_count] = trigger_ns;
                candidate_is_taker[alpha_candidate_count] = is_taker ? 1 : 0;
                ++alpha_candidate_count;
            };
            bool progressed = false;
            do {
                progressed = false;
                if (binance_ready && pending_binance.local_receive_monotonic_ns == receive_ns) {
                    (void)external_state.on_venue_event(pending_binance, external_policy);
                    binance_ready = false; refill_binance(); progressed = true;
                }
                if (coinbase_ready && pending_coinbase.local_receive_monotonic_ns == receive_ns) {
                    (void)external_state.on_venue_event(pending_coinbase, external_policy);
                    coinbase_ready = false; refill_coinbase(); progressed = true;
                }
                if (bybit_ready && pending_bybit.local_receive_monotonic_ns == receive_ns) {
                    (void)external_state.on_venue_event(pending_bybit, external_policy);
                    bybit_ready = false; refill_bybit(); progressed = true;
                }
                if (pm_ready && pending_pm.event.receive_monotonic_ns == receive_ns) {
                    const auto& event = pending_pm.event;
                    // Match against the previous consumed book, never this later update.
                    consume_arrivals(event.instrument_handle,
                        event.instrument_handle == kYes ? yes_book : no_book,
                        event.receive_monotonic_ns);

                    // Canonical same-process pure complete-set arbitrage lane.
                    // This block performs no filesystem/JSON/network I/O.  It
                    // updates the causal pair state, calls one shared native
                    // decision kernel and publishes at most one POD trace record.
                    const auto current_epoch =
                        pm_epoch.load(std::memory_order_acquire);
                    if (event.instrument_handle == kYes) {
                        yes_book = event.book;
                        yes_book_epoch = current_epoch;
                    } else if (event.instrument_handle == kNo) {
                        no_book = event.book;
                        no_book_epoch = current_epoch;
                    }
                    if (options.pure_arb_native_shadow) {
                        pure_arb::PairInput arb_input{};
                        arb_input.market_handle = kMarket;
                        arb_input.event_handle = kEvent;
                        arb_input.yes_instrument_handle = kYes;
                        arb_input.no_instrument_handle = kNo;
                        arb_input.yes_epoch = yes_book_epoch;
                        arb_input.no_epoch = no_book_epoch;
                        arb_input.market_start_wall_ms =
                            options.market_start_wall_ns / 1'000'000LL;
                        arb_input.market_end_wall_ms =
                            options.close_wall_ns / 1'000'000LL;
                        arb_input.now_wall_ms = wall_now_ns() / 1'000'000LL;
                        arb_input.trigger_receive_monotonic_ns =
                            event.frame_receive_monotonic_ns > 0
                                ? event.frame_receive_monotonic_ns
                                : event.receive_monotonic_ns;
                        arb_input.decode_complete_monotonic_ns =
                            event.decode_complete_monotonic_ns;
                        arb_input.maximum_leg_skew_ns =
                            options.pure_arb_max_leg_skew_ns;
                        arb_input.minimum_order_microunits =
                            options.min_order_microunits;
                        const auto yes_inventory =
                            authority.inventory_snapshot(kYes);
                        const auto no_inventory =
                            authority.inventory_snapshot(kNo);
                        arb_input.sell_available_microunits = std::min(
                            yes_inventory.available_microunits,
                            no_inventory.available_microunits);
                        arb_input.fee_rate = options.taker_fee_rate;
                        arb_input.fee_exponent = options.taker_fee_exponent;
                        arb_input.reserve_per_share =
                            options.pure_arb_reserve_per_share;
                        arb_input.fee_verified = 1;
                        arb_input.yes = yes_book;
                        arb_input.no = no_book;

                        auto arb_plan = pure_arb::evaluate_pair(arb_input);
                        const auto arb_finished_ns = monotonic_now_ns();
                        arb_plan.decision_monotonic_ns = arb_finished_ns;
                        ++pure_arb_shadow_evaluations;
                        pure_arb_shadow_last_buy_edge =
                            arb_plan.buy_edge_per_share;
                        pure_arb_shadow_last_sell_edge =
                            arb_plan.sell_edge_per_share;
                        pure_arb_shadow_max_buy_edge = std::max(
                            pure_arb_shadow_max_buy_edge,
                            arb_plan.buy_edge_per_share);
                        pure_arb_shadow_max_sell_edge = std::max(
                            pure_arb_shadow_max_sell_edge,
                            arb_plan.sell_edge_per_share);
                        pure_arb_shadow_last_buy_shares =
                            arb_plan.buy_economics.shares();
                        pure_arb_shadow_last_sell_shares =
                            arb_plan.sell_economics.shares();
                        if (arb_plan.buy_edge_per_share
                            > options.pure_arb_reserve_per_share + 1e-12) {
                            ++pure_arb_shadow_buy_positive;
                        }
                        if (arb_plan.sell_edge_per_share
                            > options.pure_arb_reserve_per_share + 1e-12) {
                            ++pure_arb_shadow_sell_positive;
                        }
                        if (arb_plan.buy_economics.shares_microunits
                            >= options.min_order_microunits) {
                            ++pure_arb_shadow_buy_executable;
                        }
                        if (arb_plan.sell_economics.shares_microunits
                            >= options.min_order_microunits) {
                            ++pure_arb_shadow_sell_executable;
                        }
                        if (arb_plan.reason
                            == pure_arb::DecisionReason::LegSkewExceeded) {
                            ++pure_arb_shadow_stale_pair;
                        }

                        pure_arb_shadow_last_receive_to_decision_ns =
                            std::max<std::int64_t>(
                                0, arb_finished_ns
                                    - arb_input.trigger_receive_monotonic_ns);
                        pure_arb_shadow_max_receive_to_decision_ns =
                            std::max(
                                pure_arb_shadow_max_receive_to_decision_ns,
                                pure_arb_shadow_last_receive_to_decision_ns);
                        if (pure_arb_shadow_receive_to_decision.size()
                            < pure_arb_shadow_receive_to_decision.capacity()) {
                            pure_arb_shadow_receive_to_decision.push_back(
                                pure_arb_shadow_last_receive_to_decision_ns);
                        } else {
                            ++pure_arb_shadow_latency_overflow;
                        }

                        if (latency_trace_writer) {
                            LatencyTraceRecord trace{};
                            trace.trace_id = ++pure_arb_trace_sequence;
                            if (trace.trace_id == 0)
                                trace.trace_id = ++pure_arb_trace_sequence;
                            trace.market_handle = kMarket;
                            trace.instrument_handle = event.instrument_handle;
                            trace.frame_receive_monotonic_ns =
                                arb_input.trigger_receive_monotonic_ns;
                            trace.decode_complete_monotonic_ns =
                                arb_input.decode_complete_monotonic_ns;
                            trace.arb_decision_monotonic_ns =
                                arb_finished_ns;
                            if (trace.frame_receive_monotonic_ns > 0)
                                trace.valid_mask |= TraceFrameReceive;
                            if (trace.decode_complete_monotonic_ns
                                >= trace.frame_receive_monotonic_ns
                                && trace.decode_complete_monotonic_ns > 0)
                                trace.valid_mask |= TraceDecodeComplete;
                            trace.valid_mask |= TraceArbDecision;
                            if (!latency_trace_writer->publish(trace))
                                ++pure_arb_shadow_latency_overflow;
                        }
                    }

                    if (options.capture_native_observations
                        || (options.capture_execution_windows
                            && event.receive_monotonic_ns <= execution_window_until_ns)) {
                    auto book_observation = observation(event.book, event.instrument_handle, 1);
                    book_observation.event_kind = static_cast<std::uint8_t>(event.kind);
                    book_observation.event_receive_ns = event.receive_monotonic_ns;
                    book_observation.event_exchange_ns = event.exchange_event_ns;
                    if (event.kind == MarketWsEventKind::Trade) {
                        book_observation.kind = 3;
                        book_observation.trade_e4 = event.price_e4;
                        book_observation.trade_quantity = event.quantity_microunits;
                        book_observation.trade_side = static_cast<std::uint8_t>(event.side);
                    }
                    if (!evidence_writer.publish_observation(book_observation)) ++adapter_handoff_failures;
                    }
                    if (event.kind == MarketWsEventKind::Trade
                        && event.instrument_handle != 0 && event.price_e4 > 0
                        && event.quantity_microunits > 0 && event.book.tick_size_e4 > 0) {
                        ++paper_trade_sequence;
                        if (paper_trade_sequence == 0) ++paper_trade_sequence;
                        PublicTradePrint trade{};
                        trade.trade_id = paper_trade_sequence;
                        trade.instrument_handle = event.instrument_handle;
                        trade.aggressor_side = event.side;
                        trade.price_tick = event.price_e4 / event.book.tick_size_e4;
                        trade.quantity_microunits = event.quantity_microunits;
                        trade.exchange_event_ns = event.exchange_event_ns;
                        trade.receive_monotonic_ns = event.receive_monotonic_ns;
                        const auto paper_result = paper_execution.on_public_trade(trade);
                        paper_fill_events += paper_result.fills;
                        paper_invalid_trades += paper_result.invalid;
                        for (std::size_t i = 0; i < paper_result.fills; ++i) {
                            if (!publish_fill(paper_result.records[i],
                                              ExecutionPolicyId::PassiveMaker)) {
                                ++adapter_handoff_failures;
                                break;
                            }
                            if (!publish_state(paper_result.records[i].command,
                                               ExecutionPolicyId::PassiveMaker,
                                               paper_result.records[i].order_state)) {
                                ++adapter_handoff_failures;
                                break;
                            }
                        }
                    }
                    maker::MakerDecision maker_decision;
                    bool maker_event = false;
                    if (event.book.tick_size_e4 > 0) maker_model.tick_size = event.book.tick_size_e4 / 10'000.0;
                    maker_context = native_maker_context(authority, kYes, kNo,
                        event.instrument_handle, maker_context.risk);
                    const auto maker_quantity = native_maker_admissible_quantity(
                        1'000'000, options.min_order_microunits, options.maker_share_cap_microunits,
                        event.book.best_ask_e4, limits.max_single_order_microdollars,
                        authority.capital_snapshot().available_microdollars,
                        std::min(event.book.best_bid_microunits, event.book.best_ask_microunits));
                    maker_context.risk.new_risk_frozen = maker_quantity == 0 ? 1 : 0;
                    if (maker_quantity > 0) maker_model.base_quote_shares = maker_quantity / 1'000'000.0;
                    if (event.instrument_handle == kYes) {
                        maker_decision = yes_maker.on_market_event(event, maker_context, maker_model);
                        maker_event = true;
                    } else if (event.instrument_handle == kNo) {
                        maker_decision = no_maker.on_market_event(event, maker_context, maker_model);
                        maker_event = true;
                    }
                    if (maker_event) {
                        ++maker_decisions;
                        for (std::size_t index = 0; index < maker_decision.intent_count; ++index) {
                            const auto& intent = maker_decision.intents[index];
                            if (intent.type == IntentType::CancelQuote) {
                                ++maker_cancel_intents;
                                const auto cancel = authority.cancel_maker_quote(
                                    intent.instrument_handle, intent.side, event.receive_monotonic_ns);
                                if (cancel.accepted != 0) {
                                    ++maker_cancel_handoffs;
                                    if (!paper_execution.request_cancel(
                                            cancel.command, event.receive_monotonic_ns)) {
                                        ++paper_submit_failures;
                                    }
                                } else if (cancel.reason == NativeCancelTxReason::NotCancelable) {
                                    ++maker_cancel_not_ready;
                                }
                                continue;
                            }
                            if (intent.type != IntentType::Quote) continue;
                            if (options.capture_native_decisions) {
                            auto maker_observation = observation(event.book, event.instrument_handle, 4);
                            maker_observation.decision_ns = intent.decision_monotonic_ns;
                            maker_observation.proposed_quantity = intent.quantity_microunits;
                            maker_observation.proposed_price_tick = intent.price_tick;
                            maker_observation.expected_ev = intent.expected_ev;
                            maker_observation.ev_uncertainty = intent.ev_uncertainty;
                            const double maker_fill_probability = intent.side == Side::Buy
                                ? maker_decision.bid_fill_probability
                                : maker_decision.ask_fill_probability;
                            if (std::isfinite(maker_fill_probability)
                                && maker_fill_probability >= 0.0
                                && maker_fill_probability <= 1.0) {
                                maker_observation.expected_fill_probability = maker_fill_probability;
                                maker_observation.expected_fill_probability_valid = 1;
                            }
                            maker_observation.economic_score_fill_conditioned = 1;
                            maker_observation.trade_side = static_cast<std::uint8_t>(intent.side);
                            maker_observation.reason = static_cast<std::uint8_t>(maker_decision.reason);
                            if (!evidence_writer.publish_observation(maker_observation)) ++adapter_handoff_failures;
                            }
                            if (intent.quantity_microunits < options.min_order_microunits) {
                                ++maker_inadmissible_quantity;
                                continue;
                            }
                            ExecutionPlan plan;
                            plan.intent = intent;
                            plan.tick_size_e4 = event.book.tick_size_e4;
                            plan.market_state_version = event.state_version;
                            plan.policy = ExecutionPolicyId::PassiveMaker;
                            append_candidate(plan, event.receive_monotonic_ns, false);
                            ++maker_candidates;
                        }
                    }
                    pm_ready = false; refill_pm(); progressed = true;
                }
            } while (progressed);
            if (has_external) current_signal = external_state.advance_external_cancel_signal(receive_ns, external_policy);

            const auto protect_now = monotonic_now_ns();
            const int shock_direction = fresh_shock_direction(current_signal.direction,
                current_signal.trigger_receive_monotonic_ns,
                current_signal.valid_until_monotonic_ns, protect_now);
            if (shock_direction != 0 && current_signal.signal_version > last_protective_signal) {
                last_protective_signal = current_signal.signal_version;
                protective_cancel(kYes, current_signal.direction > 0 ? Side::Sell : Side::Buy, protect_now);
                protective_cancel(kNo, current_signal.direction > 0 ? Side::Buy : Side::Sell, protect_now);
            }

            if (current_signal.signal_version != 0 && paper_execution.pending_arrivals() == 0) {
                NativeCryptoDecisionInput input;
                input.signal = current_signal;
                input.market = market;
                input.yes_book = yes_book;
                input.no_book = no_book;
                input.now_monotonic_ns = monotonic_now_ns();
                input.slow_context = slow_cache.at(input.now_monotonic_ns);
                decision_slow_context = input.slow_context;
                const bool probability_window_open = probability_model.loaded
                    && wall_now_ns() <= options.probability_evaluation_end_wall_ns;
                if (probability_window_open) {
                    input.probability = probability_model.predict(input, options.asset, options.horizon);
                    (void)probability_features(input, options.asset, options.horizon,
                        probability_model.shock_scales, decision_features);
                    probability_input_instrument = current_signal.direction > 0 ? kYes : kNo;
                    input.taker_fee_rate = options.taker_fee_rate;
                    input.taker_fee_exponent = options.taker_fee_exponent;
                    input.execution_reserve_per_share = probability_model.execution_reserve_per_share;
                    input.risk_sizing.minimum_net_edge = probability_model.minimum_net_edge;
                    input.risk_sizing.fractional_kelly = probability_model.fractional_kelly;
                    input.risk_sizing.maximum_chase_ticks = probability_model.maximum_chase_ticks;
                    input.risk_sizing.max_order_cost_microdollars = std::min(
                        limits.max_single_order_microdollars, probability_model.maximum_order_cost_microdollars);
                    input.risk_sizing.maximum_quantity_microunits = probability_model.maximum_quantity_microunits;
                    input.risk_sizing.allocated_wealth_microdollars = limits.sleeve_budget_microdollars;
                    const auto cap = authority.capital_snapshot();
                    input.risk_sizing.available_microdollars = std::max<std::int64_t>(0,
                        std::min(cap.available_microdollars, limits.max_market_exposure_microdollars - cap.total_exposure_microdollars));
                }
                const auto result = lane.construct_candidate(input);
                decision_probability = input.probability;
                decision_economics = result.economics;
                const auto finished = monotonic_now_ns();
                ++evaluations;
                // All decision reasons remain in the population tape. The
                // full PM observer supplies their historical paths; bounded
                // native repricing snapshots focus on execution candidates.
                const bool repricing_origin_eligible = result.accepted != 0
                    || result.reason == NativeCryptoDecisionReason::WeakSignal
                    || result.reason == NativeCryptoDecisionReason::InsufficientDepth
                    || result.reason == NativeCryptoDecisionReason::EntryPriceTooHigh
                    || result.reason == NativeCryptoDecisionReason::MarketAlreadyRepriced
                    || result.reason == NativeCryptoDecisionReason::ProbabilityUnavailable
                    || result.reason == NativeCryptoDecisionReason::NetEdgeNonPositive
                    || result.reason == NativeCryptoDecisionReason::RiskSizeBelowMinimum
                    || result.reason == NativeCryptoDecisionReason::SlowContextUnavailable;
                const auto repricing_instrument = result.accepted != 0
                    ? result.selected_instrument_handle
                    : (current_signal.direction > 0 ? kYes : kNo);
                const auto repricing_reason = static_cast<std::uint8_t>(result.reason);
                const bool new_execution_window_state =
                    current_signal.signal_version != last_execution_window_signal_version
                    || repricing_reason != last_execution_window_reason
                    || result.accepted != last_execution_window_accepted
                    || repricing_instrument != last_execution_window_instrument;
                if (options.capture_execution_windows && repricing_origin_eligible
                    && current_signal.signal_version != 0 && new_execution_window_state) {
                    last_execution_window_signal_version = current_signal.signal_version;
                    last_execution_window_reason = repricing_reason;
                    last_execution_window_accepted = result.accepted;
                    last_execution_window_instrument = repricing_instrument;
                    execution_window_until_ns = finished > std::numeric_limits<std::int64_t>::max()
                            - options.execution_window_ns
                        ? std::numeric_limits<std::int64_t>::max()
                        : finished + options.execution_window_ns;
                    start_repricing_window(current_signal.signal_version,
                        current_signal.trigger_receive_monotonic_ns, finished,
                        current_signal.direction, repricing_instrument);
                }
                const auto observation_reason = static_cast<std::uint8_t>(result.reason);
                if (options.capture_native_decisions && (current_signal.signal_version != last_observed_signal_version
                    || observation_reason != last_observed_reason
                    || result.accepted != last_observed_accepted
                    || finished - last_control_observation_ns >= 30'000'000'000LL)) {
                    last_control_observation_ns = finished;
                    last_observed_signal_version = current_signal.signal_version;
                    last_observed_reason = observation_reason;
                    last_observed_accepted = result.accepted;
                    const bool selected_up = result.accepted ? result.selected_yes != 0 : current_signal.direction > 0;
                    auto point = observation(selected_up ? yes_book : no_book, selected_up ? kYes : kNo, 2);
                    point.decision_ns = finished; point.reason = observation_reason; point.accepted = result.accepted;
                    point.slow_context = input.slow_context;
                    if (options.capture_execution_windows && repricing_origin_eligible) {
                        point.repricing_origin_signal_version = current_signal.signal_version;
                        decorate_pm_pair(point);
                    }
                    point.probability = input.probability;
                    point.economics = result.economics;
                    point.expected_ev = result.intent.expected_ev;
                    point.ev_uncertainty = result.intent.ev_uncertainty;
                    // Settlement probability edge is not yet a joint
                    // fill/payoff action value. Keep the distinction explicit
                    // so MAKE/TAKE arbitration cannot silently compare scales.
                    point.economic_score_fill_conditioned = 0;
                    point.probability_features = decision_features;
                    point.probability_input_instrument = probability_input_instrument;
                    point.proposed_quantity = result.intent.quantity_microunits;
                    point.proposed_price_tick = result.intent.price_tick;
                    const auto external = external_state.snapshot(input.now_monotonic_ns, external_policy);
                    point.external_valid = external.valid;
                    point.external_state_version = external.state_version;
                    point.external_input_receive_ns = external.latest_input_receive_monotonic_ns;
                    point.external_composite_price = external.venue_composite_price;
                    point.external_return_250ms = external.venue_composite_return_250ms;
                    point.external_return_1s = external.venue_composite_return_1s;
                    point.external_return_5s = external.venue_composite_return_5s;
                    point.external_return_250ms_valid = external_state.return_history_available(input.now_monotonic_ns, 250'000'000LL);
                    point.external_return_1s_valid = external_state.return_history_available(input.now_monotonic_ns, 1'000'000'000LL);
                    point.external_return_5s_valid = external_state.return_history_available(input.now_monotonic_ns, 5'000'000'000LL);
                    point.external_vol_fast = external.realized_vol_fast;
                    point.external_vol_slow = external.realized_vol_slow;
                    point.external_dispersion_bps = external.venue_dispersion_bps;
                    point.external_fresh_venues = external.venue_count_fresh;
                    if (!evidence_writer.publish_observation(point)) ++adapter_handoff_failures;
                }
                const auto reason_index = static_cast<std::size_t>(result.reason);
                if (reason_index < reasons.size()) ++reasons[reason_index];
                if (current_signal.signal_version != last_measured_signal_version) {
                    last_measured_signal_version = current_signal.signal_version;
                    if (first_signal_to_decision.size() < first_signal_to_decision.capacity()) {
                        first_signal_to_decision.push_back(std::max<std::int64_t>(
                            0, finished - current_signal.trigger_receive_monotonic_ns));
                        decision_compute.push_back(result.decision_compute_ns);
                    } else ++latency_overflow;
                }
                if (result.accepted != 0 && paper_execution.pending_arrivals() == 0) {
                    ExecutionPlan plan;
                    plan.intent = result.intent;
                    plan.tick_size_e4 = (result.selected_yes != 0 ? yes_book : no_book).tick_size_e4;
                    plan.market_state_version = result.intent.state_version;
                    plan.policy = ExecutionPolicyId::AggressiveTaker;
                    append_candidate(plan, current_signal.trigger_receive_monotonic_ns, true);
                }
            }

            // Do not cancel a toxic quote and immediately recreate that same
            // exposure from a maker candidate formed earlier in this causal cut.
            std::size_t retained_candidates = 0;
            for (std::size_t i = 0; i < alpha_candidate_count; ++i) {
                const auto& intent = alpha_candidates[i].intent;
                if (candidate_is_taker[i] == 0 && quote_is_adverse_to_shock(
                        intent.instrument_handle == kYes, intent.side, shock_direction)) {
                    ++maker_blocked_by_fast_shock;
                    continue;
                }
                alpha_candidates[retained_candidates] = alpha_candidates[i];
                candidate_trigger_ns[retained_candidates] = candidate_trigger_ns[i];
                candidate_is_taker[retained_candidates++] = candidate_is_taker[i];
            }
            alpha_candidate_count = retained_candidates;
            if (!evidence_writer.healthy()) { ++adapter_handoff_failures; break; }
            // Read-only research probes cannot reach admission or the economic ledger.
            if (options.observation_only) continue;
            // Portfolio arbitration is deliberately fail-closed until all
            // component wealth scores are on one native comparable scale.
            // Exactly one new-risk alpha candidate may reach the sole authority.
            if (alpha_candidate_count > 1) {
                ++arbitration_conflicts;
            } else if (alpha_candidate_count == 1) {
                const auto index = std::size_t{0};
                const auto authority_result = authority.submit(
                    alpha_candidates[index], options.min_order_microunits, monotonic_now_ns());
                const auto adapter_ready_ns = monotonic_now_ns();
                if (authority_result.accepted == 0) {
                    ++authority_rejections;
                    if (authority_result.reason == NativeSettlementAuthorityReason::InventoryUnavailable) {
                        ++inventory_rejections;
                    } else if (authority_result.reason == NativeSettlementAuthorityReason::BelowVenueMinimum) {
                        ++minimum_size_rejections;
                    } else if (authority_result.reason == NativeSettlementAuthorityReason::OmsDenied) {
                        ++adapter_handoff_failures;
                    } else if (authority_result.reason == NativeSettlementAuthorityReason::DuplicateMakerQuote) {
                        ++maker_duplicate_quotes;
                    } else if (authority_result.reason == NativeSettlementAuthorityReason::MakerReplacePending) {
                        ++maker_replace_pending;
                        if (authority_result.cancel.accepted != 0) {
                            ++maker_cancel_handoffs;
                            if (!paper_execution.request_cancel(
                                    authority_result.cancel.command, adapter_ready_ns)) {
                                ++paper_submit_failures;
                            }
                        } else if (authority_result.cancel.reason == NativeCancelTxReason::NotCancelable) {
                            ++maker_cancel_not_ready;
                        }
                    }
                } else {
                    // Only the sole new-risk owner consumes a taker signal.
                    // Arbitration conflicts and admission rejection leave a
                    // still-fresh signal available for causal reevaluation.
                    if (candidate_is_taker[index] != 0) {
                        lane.commit_signal(kMarket, current_signal.signal_version);
                    }
                    const auto& paper_book = authority_result.tx.command.instrument_handle == kYes
                        ? yes_book : no_book;
                    const auto paper_result = paper_execution.submit(
                        authority_result.tx.command, paper_book, adapter_ready_ns);
                    if (paper_result.reason == NativePaperReason::LifecycleFailure
                        || paper_result.reason == NativePaperReason::InvalidCommand) {
                        ++paper_submit_failures;
                        ++adapter_handoff_failures;
                        break;
                    }
                    if (!publish_order(authority_result.tx.command,
                                       alpha_candidates[index].policy,
                                       paper_book.exchange_event_ns,
                                       paper_book.receive_monotonic_ns)) {
                        ++adapter_handoff_failures;
                        break;
                    }
                    if (paper_result.filled_microunits == 0
                        && (paper_result.final_state == OrderState::Rejected
                            || paper_result.final_state == OrderState::Expired)) {
                        if (!publish_state(authority_result.tx.command,
                                           alpha_candidates[index].policy,
                                           paper_result.final_state, paper_result.reason,
                                           paper_result.censored != 0)) {
                            ++adapter_handoff_failures;
                            break;
                        }
                    }
                    ++accepted;
                    paper_arrival_censored += paper_result.censored != 0;
                    if (paper_result.accepted != 0 && paper_result.filled_microunits > 0) {
                        ++paper_fill_events;
                        if (!publish_fill(paper_result.fill,
                                          alpha_candidates[index].policy)
                            || !publish_state(authority_result.tx.command,
                                              alpha_candidates[index].policy,
                                              paper_result.fill.order_state,
                                              paper_result.reason)) {
                            ++adapter_handoff_failures;
                            break;
                        }
                        if (paper_result.final_state != paper_result.fill.order_state
                            && !publish_state(authority_result.tx.command,
                                              alpha_candidates[index].policy,
                                              paper_result.final_state,
                                              paper_result.reason)) {
                            ++adapter_handoff_failures;
                            break;
                        }
                    }
                    if (candidate_is_taker[index] != 0) {
                        ++taker_accepted;
                        if (paper_result.filled_microunits > 0) lane.mark_market_traded(kMarket);
                        if (accepted_signal_to_admission.size() < accepted_signal_to_admission.capacity()) {
                            const auto trigger_ns = candidate_trigger_ns[index];
                            accepted_signal_to_admission.push_back(std::max<std::int64_t>(
                                0, authority_result.tx.command.queue_monotonic_ns - trigger_ns));
                            accepted_signal_to_adapter.push_back(std::max<std::int64_t>(
                                0, adapter_ready_ns - trigger_ns));
                        } else ++latency_overflow;
                    } else {
                        ++maker_accepted;
                    }
                }
            }
        }

        // Market rollover is a control-plane boundary, but every PAPER
        // order must be terminal before this native authority can disappear.
        // Cancel all live maker quotes and advance beyond the bounded PAPER
        // cancel latency; never carry an unowned reservation into the next market.
        const auto shutdown_cancel_ns = monotonic_now_ns();
        // A stopped capture cannot establish execution. Retire pending
        // research arrivals as censored, never as fills or observed nonfills.
        paper_execution.invalidate_arrivals();
        for (const auto instrument : {kYes, kNo})
            consume_arrivals(instrument, BookHotSnapshot{}, std::numeric_limits<std::int64_t>::max());
        for (const auto instrument : {kYes, kNo}) {
            for (const auto side : {Side::Buy, Side::Sell}) {
                const auto cancel = authority.cancel_maker_quote(
                    instrument, side, shutdown_cancel_ns);
                if (cancel.accepted != 0) {
                    if (!paper_execution.request_cancel(cancel.command, shutdown_cancel_ns)) {
                        ++adapter_handoff_failures;
                    }
                } else if (cancel.reason != NativeCancelTxReason::UnknownClientOrder
                           && cancel.reason != NativeCancelTxReason::DuplicateNoop) {
                    const auto* current = cancel.command.client_order_id != 0
                        ? adapter_endpoint.find(cancel.command.client_order_id) : nullptr;
                    if (current != nullptr && !(
                            current->state == OrderState::Filled
                            || current->state == OrderState::Cancelled
                            || current->state == OrderState::Rejected
                            || current->state == OrderState::Expired
                            || current->state == OrderState::Lost)) {
                        ++adapter_handoff_failures;
                    }
                }
            }
        }
        const auto shutdown_advance = paper_execution.advance_time(
            shutdown_cancel_ns + 100'000'001LL);
        if (shutdown_advance.invalid != 0) {
            ++adapter_handoff_failures;
        }
        for (std::size_t i = 0; i < shutdown_advance.cancellation_count; ++i) {
            if (!publish_state(shutdown_advance.cancellations[i].command,
                               ExecutionPolicyId::PassiveMaker,
                               OrderState::Cancelled)) {
                ++adapter_handoff_failures;
            }
        }
        if (authority.active_orders() != 0 || paper_execution.resting_orders() != 0) {
            ++adapter_handoff_failures;
        }

        // Stop all producers before waiting for any feed to join. Otherwise a
        // slow PM shutdown fills an external queue after its consumer has left.
#if defined(__APPLE__)
        stopping.store(true, std::memory_order_release);
#else
        stopping.request_stop();
#endif
        pm_feed.stop();
        binance_thread.join();
        if (coinbase_thread.joinable()) coinbase_thread.join();
        slow_feed.stop();
        if (bybit_thread.joinable()) bybit_thread.join();
        evidence_writer.stop();
        if (latency_trace_writer) latency_trace_writer->stop();
        const auto latency_trace_status = latency_trace_writer
            ? latency_trace_writer->snapshot()
            : LatencyTraceWriterSnapshot{};

        const auto binance_status = binance.snapshot();
        const auto coinbase_status = coinbase ? coinbase->snapshot() : ExternalWsSnapshot{};
        const auto bybit_status = bybit ? bybit->snapshot() : ExternalWsSnapshot{};
        const auto coinbase_l2_status = coinbase_l2
            ? coinbase_l2->metrics() : CoinbaseL2Metrics{};
        const auto coinbase_l2_diagnostic = coinbase_l2
            ? coinbase_l2->diagnostic() : std::string("UNAVAILABLE_FOR_ASSET");
        const auto pm_status = pm_feed.snapshot();
        const auto binance_ingress_status = binance_ingress.snapshot();
        const auto coinbase_ingress_status = coinbase_ingress.snapshot();
        const auto bybit_ingress_status = bybit_ingress.snapshot();
        const bool clean = pm_drops.load() == 0
            && binance_ingress_status.dropped_events == 0
            && coinbase_ingress_status.dropped_events == 0
            && bybit_ingress_status.dropped_events == 0
            && adapter_handoff_failures == 0
            && evidence_writer.healthy()
            && evidence_writer.dropped() == 0
            && evidence_writer.published() == evidence_writer.written();

        std::cout << json::serialize(json::object{
            {"schema", "polymarket_v7_crypto_settlement_native_candidate_v2"},
            {"paper_only", true}, {"authenticated_execution", false},
            {"real_order_submission", false}, {"real_capital_at_risk", false},
            {"authority", options.observation_only ? "ZERO_AUTHORITY_RESEARCH" : "PAPER_SIMULATED_SINGLE_OWNER"},
            {"observation_only", options.observation_only},
            {"native_decision_capture_enabled", options.capture_native_decisions},
            {"native_full_observation_capture_enabled", options.capture_native_observations},
            {"execution_window_capture_enabled", options.capture_execution_windows},
            {"execution_window_ns", options.execution_window_ns},
            {"repricing_origins", repricing_origins}, {"repricing_labels", repricing_labels},
            {"repricing_censors", repricing_censors},
            {"repricing_window_overflow", repricing_window_overflow},
            {"repricing_evidence_compute_ns", repricing_evidence_compute_ns},
            {"repricing_evidence_max_ns", repricing_evidence_max_ns},
            {"repricing_horizons_ms", json::array{
                5, 10, 25, 50, 100, 250, 500, 750, 1000,
                1250, 1500, 1750, 2000, 3000, 4000, 5000, 7500, 10000}},
            {"native_observation_capture_mode", options.capture_native_observations
                ? "FULL" : options.capture_execution_windows ? "DECISION_WINDOWS"
                : options.capture_native_decisions ? "DECISIONS" : "NONE"},
            {"asset", options.asset}, {"horizon", options.horizon},
            {"critical_path", "CPP_SAME_PROCESS_FEED_DECODE_TO_SINGLE_SETTLEMENT_AUTHORITY"},
            {"temporal_architecture", "FAST_NATIVE_PLUS_ASYNC_VERSIONED_CONTEXT"},
            {"slow_context_updates", slow_context_updates},
            {"slow_context_failures", slow_feed.failures()},
            {"slow_context_overflows", slow_feed.overflows()},
            {"slow_context_model_used_mask", 0},
            {"external_protective_cancels", external_protective_cancels},
            {"maker_blocked_by_fast_shock", maker_blocked_by_fast_shock},
            {"clean_capture", clean}, {"duration_seconds", options.duration_seconds},
            {"evaluations", evaluations}, {"accepted_candidates", accepted},
            {"taker_accepted", taker_accepted}, {"maker_accepted", maker_accepted},
            {"maker_decisions", maker_decisions}, {"maker_candidates", maker_candidates},
            {"maker_inadmissible_quantity", maker_inadmissible_quantity},
            {"maker_quote_share_cap", maker_context.risk.max_quote_shares},
            {"maker_model_valid_cells", std::count_if(maker_model.execution_cells.begin(),
                maker_model.execution_cells.end(), [](const auto& cell) { return cell.valid != 0; })},
            {"maker_cancel_intents", maker_cancel_intents},
            {"maker_cancel_handoffs", maker_cancel_handoffs},
            {"maker_cancel_not_ready", maker_cancel_not_ready},
            {"maker_duplicate_quotes", maker_duplicate_quotes},
            {"maker_replace_pending", maker_replace_pending},
            {"paper_resting_orders", paper_execution.resting_orders()},
            {"paper_synthetic_acks", paper_execution.synthetic_acks()},
            {"paper_fill_events", paper_fill_events},
            {"paper_adapter_fills", paper_execution.paper_fills()},
            {"paper_pending_arrivals", paper_execution.pending_arrivals()},
            {"paper_arrival_censored", paper_arrival_censored},
            {"paper_arrival_observed_nonfills", paper_arrival_observed_nonfills},
            {"paper_venue_delay_ns", options.paper_venue_delay_ns},
            {"paper_assumed_transport_delay_ns", options.paper_assumed_transport_delay_ns},
            {"paper_terms_sha256", options.paper_terms_sha256},
            {"paper_cancels", paper_execution.paper_cancels()},
            {"paper_invalid_trades", paper_invalid_trades},
            {"paper_submit_failures", paper_submit_failures},
            {"evidence_healthy", evidence_writer.healthy()},
            {"evidence_published", evidence_writer.published()},
            {"evidence_written", evidence_writer.written()},
            {"evidence_dropped", evidence_writer.dropped()},
            {"arbitration_conflicts_fail_closed", arbitration_conflicts},
            {"authority_rejections", authority_rejections},
            {"inventory_rejections", inventory_rejections},
            {"minimum_size_rejections", minimum_size_rejections},
            {"pure_arb_native_shadow", json::object{
                {"enabled", options.pure_arb_native_shadow},
                {"authority", "ZERO_AUTHORITY_RESEARCH_ONLY"},
                {"execution_handoff", false},
                {"reserve_per_share", options.pure_arb_reserve_per_share},
                {"maximum_leg_skew_ns", options.pure_arb_max_leg_skew_ns},
                {"evaluations", pure_arb_shadow_evaluations},
                {"buy_after_reserve_positive", pure_arb_shadow_buy_positive},
                {"sell_after_reserve_positive", pure_arb_shadow_sell_positive},
                {"buy_min_order_executable", pure_arb_shadow_buy_executable},
                {"sell_min_order_executable", pure_arb_shadow_sell_executable},
                {"stale_pair_rejections", pure_arb_shadow_stale_pair},
                {"latency_sample_overflow", pure_arb_shadow_latency_overflow},
                {"last_buy_edge_per_share", pure_arb_shadow_last_buy_edge},
                {"last_sell_edge_per_share", pure_arb_shadow_last_sell_edge},
                {"max_buy_edge_per_share", pure_arb_shadow_max_buy_edge},
                {"max_sell_edge_per_share", pure_arb_shadow_max_sell_edge},
                {"last_buy_shares", pure_arb_shadow_last_buy_shares},
                {"last_sell_shares", pure_arb_shadow_last_sell_shares},
                {"last_receive_to_decision_ns",
                    pure_arb_shadow_last_receive_to_decision_ns},
                {"max_receive_to_decision_ns",
                    pure_arb_shadow_max_receive_to_decision_ns},
                {"receive_to_decision",
                    latency_distribution(std::move(
                        pure_arb_shadow_receive_to_decision))}}},
            {"adapter_handoff_failures", adapter_handoff_failures},
            {"native_oms_active_orders", authority.active_orders()},
            {"adapter_unsent_observations", adapter_endpoint.observed_unsent()},
            {"adapter_healthy", adapter_endpoint.healthy()},
            {"network_orders_sent", 0},
            {"simulated_fills", paper_execution.paper_fills()},
            {"latency_sample_overflow", latency_overflow},
            {"binary_latency_trace", json::object{
                {"configured", static_cast<bool>(latency_trace_writer)},
                {"healthy", latency_trace_status.healthy != 0},
                {"published", latency_trace_status.published},
                {"written", latency_trace_status.written},
                {"dropped", latency_trace_status.dropped},
                {"queued", latency_trace_status.queued}}},
            {"accepted_signal_to_admission", latency_distribution(std::move(accepted_signal_to_admission))},
            {"accepted_signal_to_adapter", latency_distribution(std::move(accepted_signal_to_adapter))},
            {"first_signal_to_decision", latency_distribution(std::move(first_signal_to_decision))},
            {"decision_compute", latency_distribution(std::move(decision_compute))},
            {"reason_counts", reason_json(reasons)},
            {"binance", {{"invalid_frames", binance_ingress_status.invalid_frames}, {"enqueued", binance_ingress_status.enqueued_events}, {"drained", binance_ingress_status.drained_events}, {"queued", binance_ingress_status.queued}, {"frames", binance_status.frames_received}, {"transport_failures", binance_status.transport_failures}, {"drops", binance_ingress_status.dropped_events}}},
            {"coinbase_l2", {{"available", static_cast<bool>(coinbase_l2)},
                {"valid", coinbase_l2_status.valid != 0},
                {"updates", coinbase_l2_status.update_count},
                {"parse_failures", coinbase_l2_status.parse_failures},
                {"diagnostic", coinbase_l2_diagnostic}}},
            {"coinbase", {{"invalid_frames", coinbase_ingress_status.invalid_frames}, {"enqueued", coinbase_ingress_status.enqueued_events}, {"drained", coinbase_ingress_status.drained_events}, {"queued", coinbase_ingress_status.queued}, {"frames", coinbase_status.frames_received}, {"transport_failures", coinbase_status.transport_failures}, {"drops", coinbase_ingress_status.dropped_events}}},
            {"bybit_confirmation", {{"enabled", static_cast<bool>(bybit)}, {"invalid_frames", bybit_ingress_status.invalid_frames}, {"enqueued", bybit_ingress_status.enqueued_events}, {"drained", bybit_ingress_status.drained_events}, {"queued", bybit_ingress_status.queued}, {"frames", bybit_status.frames_received}, {"transport_failures", bybit_status.transport_failures}, {"drops", bybit_ingress_status.dropped_events}}},
            {"probability_model_configured", probability_model.loaded},
            {"direction_only_fallback_allowed", !options.strict_signal_policy},
            {"probability_evaluation_end_wall_ns",
                probability_model.loaded ? json::value(options.probability_evaluation_end_wall_ns) : json::value(nullptr)},
            {"probability_evaluation_open",
                probability_model.loaded && wall_now_ns() <= options.probability_evaluation_end_wall_ns},
            {"signal_policy", {{"minimum_binance_return_bp", options.minimum_absolute_binance_return_bp},
                {"minimum_confirmation_return_bp", options.minimum_absolute_confirmation_return_bp},
                {"maximum_signal_age_ns", options.maximum_signal_age_ns},
                {"confirmation_venue", options.confirmation_venue},
                {"require_pm_book_pre_signal", options.strict_signal_policy}}},
            {"polymarket", {{"messages", pm_status.messages}, {"reconnects", pm_status.reconnects}, {"errors", pm_status.errors}, {"drops", pm_drops.load()}}},
            {"note", "PAPER-only native candidate. Maker and taker share one in-process inventory/capital/OMS authority. Taker research fills use delayed local-receive arrival-price FAK with bounded visible-depth partial fills, not exchange-confirmed execution; maker fills use pessimistic public-print queue depletion and bounded cancel latency. No authenticated submission or real capital is possible."}
        }) << '\n';
        return clean ? 0 : 2;
    } catch (const std::exception& error) {
        std::cerr << "crypto_settlement_native_candidate: " << error.what() << '\n';
        return 64;
    }
}
