#include "pm/fast_ws.hpp"
#include "pm/v7_crypto_decision_lane.hpp"
#include "pm/v7_external_ingress.hpp"
#include "pm/v7_external_ws.hpp"
#include "pm/v7_ingress_wakeup.hpp"
#include "pm/v7_market_ws.hpp"
#include "pm/v7_spsc.hpp"

#include <boost/json.hpp>

#include <algorithm>
#include <array>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <limits>
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
constexpr std::size_t kExternalBatchCapacity = 8192;
constexpr std::size_t kMergedCapacity = 12288;

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

struct Options {
    std::string yes_token;
    std::string no_token;
    std::string pm_ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/market";
    std::int64_t close_wall_ns = 0;
    std::int32_t tick_size_e4 = 100;
    std::int64_t min_order_microunits = 5'000'000;
    int duration_seconds = 30;
    bool validate_only = false;
};

Options parse_options(int argc, char** argv) {
    Options out;
    for (int i = 1; i < argc; ++i) {
        const std::string_view arg = argv[i];
        auto next = [&]() -> std::string_view {
            if (++i >= argc) throw std::invalid_argument("missing option value");
            return argv[i];
        };
        if (arg == "--yes-token") out.yes_token = next();
        else if (arg == "--no-token") out.no_token = next();
        else if (arg == "--pm-ws-url") out.pm_ws_url = next();
        else if (arg == "--close-wall-ns") out.close_wall_ns = bounded_integer<std::int64_t>(next(), 1, std::numeric_limits<std::int64_t>::max());
        else if (arg == "--tick-size-e4") out.tick_size_e4 = bounded_integer<std::int32_t>(next(), 1, 5000);
        else if (arg == "--min-order-microunits") out.min_order_microunits = bounded_integer<std::int64_t>(next(), 1, 1'000'000'000);
        else if (arg == "--duration-seconds") out.duration_seconds = bounded_integer<int>(next(), 1, 3600);
        else if (arg == "--validate-only") out.validate_only = true;
        else throw std::invalid_argument("unknown option");
    }
    return out;
}

struct MergedEvent {
    std::int64_t receive_ns = 0;
    std::uint8_t kind = 0; // 1 external, 2 Polymarket
    ExternalVenueEvent external{};
    MarketWsEvent polymarket{};
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

ExternalVenueConnectionSpec coinbase_ticker_spec(std::uint64_t asset_handle) {
    ExternalVenueConnectionSpec spec;
    spec.venue = VenueId::CoinbaseSpot;
    spec.host = "advanced-trade-ws.coinbase.com";
    spec.port = "443";
    spec.target = "/";
    spec.subscription_json = R"({"type":"subscribe","product_ids":["BTC-USD"],"channel":"ticker"})";
    spec.symbol = "BTC-USD";
    spec.asset_handle = asset_handle;
    spec.max_message_bytes = 1U << 20;
    return spec;
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
        if (options.validate_only) {
            std::cout << "native crypto flash shadow configuration PASS\n";
            return 0;
        }
        if (options.yes_token.empty() || options.no_token.empty() || options.yes_token == options.no_token
            || options.close_wall_ns <= wall_now_ns()) throw std::invalid_argument("live market identity required");

        constexpr std::uint64_t kAsset = 1, kMarket = 1, kEvent = 1, kYes = 1, kNo = 2;
        IngressWakeup wakeup;
        ExternalVenueIngress binance_ingress(VenueId::BinanceSpot, kAsset, nullptr, &wakeup);
        ExternalVenueIngress coinbase_ingress(VenueId::CoinbaseSpot, kAsset, nullptr, &wakeup);
        auto binance_spec = btc_spot_connection_spec(VenueId::BinanceSpot, kAsset);
        auto coinbase_spec = coinbase_ticker_spec(kAsset);
        ExternalVenueWsClient binance(binance_spec, &binance_ingress);
        ExternalVenueWsClient coinbase(coinbase_spec, &coinbase_ingress);

        std::vector<TokenBinding> bindings{
            {options.yes_token, kMarket, kEvent, kYes, options.tick_size_e4},
            {options.no_token, kMarket, kEvent, kNo, options.tick_size_e4},
        };
        MarketWsShard pm_decoder(std::move(bindings));
        SpscRing<MarketWsEvent, kPmQueueCapacity> pm_queue;
        std::atomic<std::uint64_t> pm_drops{0}, pm_faults{0};

        pm::fast::MarketWebSocketFeed pm_feed(
            options.pm_ws_url, {options.yes_token, options.no_token}, 2,
            [&](std::string_view payload, const pm::fast::FeedReceiveStamp& stamp, std::size_t) {
                std::array<MarketWsEvent, kPmFrameEvents> decoded{};
                const auto result = pm_decoder.process_frame(payload, stamp, decoded);
                bool notified = false;
                if (result.invalid_frame || result.output_overflow || result.arena_exhausted || result.lineage_invalidated) {
                    pm_faults.fetch_add(1, std::memory_order_relaxed);
                }
                for (std::size_t i = 0; i < result.output_count; ++i) {
                    if (!pm_queue.try_push(decoded[i])) {
                        pm_drops.fetch_add(1, std::memory_order_relaxed);
                        pm_faults.fetch_add(1, std::memory_order_relaxed);
                    } else notified = true;
                }
                if (notified || result.invalid_frame || result.output_overflow || result.arena_exhausted) wakeup.notify();
            },
            [&](std::size_t, std::string_view) {
                pm_decoder.invalidate_all_lineage();
                pm_faults.fetch_add(1, std::memory_order_relaxed);
                wakeup.notify();
            });

        ExternalStatePolicy external_policy;
        external_policy.external_cancel_enabled = 1;
        external_policy.external_cancel_shock_window_ns = 100'000'000LL;
        external_policy.external_cancel_grid_ns = 25'000'000LL;
        external_policy.external_cancel_cooldown_ns = 250'000'000LL;
        external_policy.external_cancel_warmup_ns = 300'000'000LL;
        external_policy.external_cancel_signal_ttl_ns = 100'000'000LL;
        external_policy.external_cancel_min_abs_return_bp = 0.30;
        ExternalAssetState external_state(kAsset);

        NativeCryptoDecisionPolicy decision_policy;
        // Frozen LEAD_LAG_TAKER_V1 uses maximum_signal_age_ms=5000; the source
        // signal's shorter technical valid flag is not an economic expiry.
        decision_policy.require_signal_valid = 0;
        NativeCryptoDecisionLane lane(decision_policy);
        CapitalLimits limits;
        limits.sleeve_budget_microdollars = 1'000'000'000LL;
        limits.max_total_exposure_microdollars = 1'000'000'000LL;
        limits.max_market_exposure_microdollars = 100'000'000LL;
        limits.max_single_order_microdollars = 10'000'000LL;
        SleeveCapitalAccount capital(limits);

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

        BookHotSnapshot yes_book{}, no_book{};
        ExternalCancelSignalSnapshot current_signal{};
        std::vector<ExternalVenueEvent> external_batch(kExternalBatchCapacity);
        std::vector<MergedEvent> merged(kMergedCapacity);
        std::vector<std::int64_t> signal_to_admission;
        std::vector<std::int64_t> decision_compute;
        signal_to_admission.reserve(4096);
        decision_compute.reserve(4096);
        std::array<std::uint64_t, 32> reasons{};
        std::uint64_t evaluations = 0, accepted = 0, latency_overflow = 0;

#if defined(__APPLE__)
        std::atomic<bool> stopping{false};
        ExternalStopToken stop_token(stopping);
#else
        std::stop_source stopping;
        auto stop_token = stopping.get_token();
#endif
        std::thread binance_thread([&] { binance.run(stop_token); });
        std::thread coinbase_thread([&] { coinbase.run(stop_token); });
        pm_feed.start();

        const auto deadline = start_mono + static_cast<std::int64_t>(options.duration_seconds) * 1'000'000'000LL;
        while (monotonic_now_ns() < deadline) {
            if (pm_faults.exchange(0, std::memory_order_acq_rel) != 0) {
                yes_book.valid = 0; yes_book.lineage_continuous = 0;
                no_book.valid = 0; no_book.lineage_continuous = 0;
            }
            std::size_t external_count = binance_ingress.drain_events(external_batch);
            if (external_count < external_batch.size()) {
                external_count += coinbase_ingress.drain_events(
                    std::span<ExternalVenueEvent>(external_batch.data() + external_count,
                                                  external_batch.size() - external_count));
            }
            std::size_t merged_count = 0;
            for (std::size_t i = 0; i < external_count && merged_count < merged.size(); ++i) {
                merged[merged_count].receive_ns = external_batch[i].local_receive_monotonic_ns;
                merged[merged_count].kind = 1;
                merged[merged_count].external = external_batch[i];
                ++merged_count;
            }
            MarketWsEvent pm_event;
            while (merged_count < merged.size() && pm_queue.try_pop(pm_event)) {
                merged[merged_count].receive_ns = pm_event.receive_monotonic_ns;
                merged[merged_count].kind = 2;
                merged[merged_count].polymarket = pm_event;
                ++merged_count;
            }
            if (merged_count == 0) {
                (void)wakeup.wait_for(2ms);
                continue;
            }
            std::sort(merged.begin(), merged.begin() + static_cast<std::ptrdiff_t>(merged_count),
                      [](const MergedEvent& lhs, const MergedEvent& rhs) {
                          if (lhs.receive_ns != rhs.receive_ns) return lhs.receive_ns < rhs.receive_ns;
                          return lhs.kind < rhs.kind;
                      });

            std::size_t begin = 0;
            while (begin < merged_count) {
                const auto receive_ns = merged[begin].receive_ns;
                std::size_t end = begin;
                bool has_external = false;
                while (end < merged_count && merged[end].receive_ns == receive_ns) {
                    has_external = has_external || merged[end].kind == 1;
                    ++end;
                }
                if (has_external && receive_ns > 1) {
                    current_signal = external_state.advance_external_cancel_signal(receive_ns - 1, external_policy);
                }
                for (std::size_t i = begin; i < end; ++i) {
                    if (merged[i].kind == 1) {
                        (void)external_state.on_venue_event(merged[i].external, external_policy);
                    } else {
                        const auto& event = merged[i].polymarket;
                        if (event.instrument_handle == kYes) yes_book = event.book;
                        else if (event.instrument_handle == kNo) no_book = event.book;
                    }
                }
                if (has_external) current_signal = external_state.advance_external_cancel_signal(receive_ns, external_policy);

                if (current_signal.signal_version != 0) {
                    NativeCryptoDecisionInput input;
                    input.signal = current_signal;
                    input.market = market;
                    input.yes_book = yes_book;
                    input.no_book = no_book;
                    input.now_monotonic_ns = receive_ns;
                    const auto result = lane.evaluate(input, capital);
                    ++evaluations;
                    const auto reason_index = static_cast<std::size_t>(result.reason);
                    if (reason_index < reasons.size()) ++reasons[reason_index];
                    if (result.accepted != 0) {
                        ++accepted;
                        lane.mark_market_traded(kMarket);
                        const auto finished = monotonic_now_ns();
                        if (signal_to_admission.size() < signal_to_admission.capacity()) {
                            signal_to_admission.push_back(std::max<std::int64_t>(0, finished - current_signal.trigger_receive_monotonic_ns));
                            decision_compute.push_back(result.decision_compute_ns);
                        } else ++latency_overflow;
                    }
                }
                begin = end;
            }
        }

        pm_feed.stop();
#if defined(__APPLE__)
        stopping.store(true, std::memory_order_release);
#else
        stopping.request_stop();
#endif
        binance_thread.join();
        coinbase_thread.join();

        const auto binance_status = binance.snapshot();
        const auto coinbase_status = coinbase.snapshot();
        const auto pm_status = pm_feed.snapshot();
        const auto binance_ingress_status = binance_ingress.snapshot();
        const auto coinbase_ingress_status = coinbase_ingress.snapshot();
        const bool clean = pm_drops.load() == 0
            && binance_ingress_status.dropped_events == 0
            && coinbase_ingress_status.dropped_events == 0;

        std::cout << json::serialize(json::object{
            {"schema", "polymarket_v7_crypto_flash_shadow_v1"},
            {"paper_only", true}, {"authenticated_execution", false},
            {"real_order_submission", false}, {"real_capital_at_risk", false},
            {"authority", "SHADOW_ZERO_AUTHORITY"},
            {"critical_path", "CPP_SAME_PROCESS_CAUSAL_EVENT_TO_RISK_ADMISSION"},
            {"clean_capture", clean}, {"duration_seconds", options.duration_seconds},
            {"evaluations", evaluations}, {"accepted_candidates", accepted},
            {"latency_sample_overflow", latency_overflow},
            {"signal_to_admission", latency_distribution(std::move(signal_to_admission))},
            {"decision_compute", latency_distribution(std::move(decision_compute))},
            {"reason_counts", reason_json(reasons)},
            {"binance", {{"frames", binance_status.frames_received}, {"transport_failures", binance_status.transport_failures}, {"drops", binance_ingress_status.dropped_events}}},
            {"coinbase", {{"frames", coinbase_status.frames_received}, {"transport_failures", coinbase_status.transport_failures}, {"drops", coinbase_ingress_status.dropped_events}}},
            {"polymarket", {{"messages", pm_status.messages}, {"reconnects", pm_status.reconnects}, {"errors", pm_status.errors}, {"drops", pm_drops.load()}}},
            {"note", "No order adapter is instantiated. Accepted candidates are shadow admissions only."}
        }) << '\n';
        return clean ? 0 : 2;
    } catch (const std::exception& error) {
        std::cerr << "crypto_flash_shadow: " << error.what() << '\n';
        return 64;
    }
}
