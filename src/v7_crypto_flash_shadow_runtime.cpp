#include "pm/fast_ws.hpp"
#include "pm/thread_tuning.hpp"
#include "pm/v7_coinbase_l2_observer.hpp"
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
    int idle_spin_us = 50;
    int socket_busy_poll_us = 0;
    int cpu_pin = 1;
    int dynamic_rx_align = 1;
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
        else if (arg == "--idle-spin-us") out.idle_spin_us = bounded_integer<int>(next(), 0, 2000);
        else if (arg == "--socket-busy-poll-us") out.socket_busy_poll_us = bounded_integer<int>(next(), 0, 2000);
        else if (arg == "--cpu-pin") out.cpu_pin = bounded_integer<int>(next(), 0, 1);
        else if (arg == "--dynamic-rx-align") out.dynamic_rx_align = bounded_integer<int>(next(), 0, 1);
        else if (arg == "--validate-only") out.validate_only = true;
        else throw std::invalid_argument("unknown option");
    }
    return out;
}

struct PmQueuedEvent {
    MarketWsEvent event{};
    std::uint64_t connection_epoch = 0;
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
        if (options.validate_only) {
            std::cout << "native crypto flash shadow configuration PASS\n";
            return 0;
        }
        if (options.yes_token.empty() || options.no_token.empty() || options.yes_token == options.no_token
            || options.close_wall_ns <= wall_now_ns()) throw std::invalid_argument("live market identity required");

        // Role order: decision owner, Binance ingress, Coinbase ingress,
        // Polymarket market-data worker. On Linux select the highest allowed
        // CPUs, leaving the lower CPUs for OS/housekeeping work. The London HFT
        // host policy supplies no-SMT physical cores; on other hosts this stays
        // observable and can be disabled with --cpu-pin 0.
        std::array<int, 4> role_cpus{-1, -1, -1, -1};
        const auto allowed_cpus = pm::threading::allowed_cpu_ids();
        const auto selected_cpus = options.cpu_pin
            ? pm::threading::select_dedicated_cpus(allowed_cpus, role_cpus.size())
            : std::vector<int>{};
        if (selected_cpus.size() == role_cpus.size()) {
            std::copy(selected_cpus.begin(), selected_cpus.end(), role_cpus.begin());
        }
        const bool cpu_pin_active = selected_cpus.size() == role_cpus.size();
        std::vector<int> feed_rx_cpus;
        if (cpu_pin_active && options.dynamic_rx_align != 0)
            feed_rx_cpus.assign(role_cpus.begin() + 1, role_cpus.end());
        const bool dynamic_rx_requested = !feed_rx_cpus.empty();
        std::atomic<std::uint64_t> affinity_failures{0};

        constexpr std::uint64_t kAsset = 1, kMarket = 1, kEvent = 1, kYes = 1, kNo = 2;
        IngressWakeup wakeup;
        ExternalVenueIngress binance_ingress(VenueId::BinanceSpot, kAsset, nullptr, &wakeup);
        ExternalVenueIngress coinbase_ingress(VenueId::CoinbaseSpot, kAsset, nullptr, &wakeup);
        auto binance_spec = btc_spot_connection_spec(VenueId::BinanceSpot, kAsset);
        auto coinbase_spec = btc_spot_connection_spec(VenueId::CoinbaseSpot, kAsset);
        binance_spec.socket_busy_poll_us = options.socket_busy_poll_us;
        coinbase_spec.socket_busy_poll_us = options.socket_busy_poll_us;
        binance_spec.dynamic_rx_cpu_allowlist = feed_rx_cpus;
        coinbase_spec.dynamic_rx_cpu_allowlist = feed_rx_cpus;
        CoinbaseL2FrameObserver coinbase_l2(coinbase_ingress, kAsset);
        ExternalVenueWsClient binance(binance_spec, &binance_ingress);
        ExternalVenueWsClient coinbase(coinbase_spec, nullptr, &coinbase_l2);

        std::vector<TokenBinding> bindings{
            {options.yes_token, kMarket, kEvent, kYes, options.tick_size_e4},
            {options.no_token, kMarket, kEvent, kNo, options.tick_size_e4},
        };
        MarketWsShard pm_decoder(std::move(bindings));
        SpscRing<PmQueuedEvent, kPmQueueCapacity> pm_queue;
        std::atomic<std::uint64_t> pm_drops{0}, pm_faults{0};
        std::atomic<std::uint64_t> pm_epoch{1};

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
            },
            role_cpus[3] >= 0 ? std::vector<int>{role_cpus[3]} : std::vector<int>{},
            options.socket_busy_poll_us, feed_rx_cpus);

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
        ExternalVenueEvent pending_binance{}, pending_coinbase{};
        PmQueuedEvent pending_pm{};
        bool binance_ready = false, coinbase_ready = false, pm_ready = false;
        std::vector<std::int64_t> accepted_signal_to_admission;
        std::vector<std::int64_t> first_signal_to_decision;
        std::vector<std::int64_t> decision_compute;
        accepted_signal_to_admission.reserve(4096);
        first_signal_to_decision.reserve(4096);
        decision_compute.reserve(4096);
        std::array<std::uint64_t, 32> reasons{};
        std::uint64_t evaluations = 0, accepted = 0, latency_overflow = 0;
        std::uint64_t last_measured_signal_version = 0;

#if defined(__APPLE__)
        std::atomic<bool> stopping{false};
        ExternalStopToken stop_token(stopping);
#else
        std::stop_source stopping;
        auto stop_token = stopping.get_token();
#endif
        const auto pin_role = [&](int cpu) noexcept {
            if (cpu >= 0 && pm::threading::pin_current_thread_to_cpu(cpu) != 0) {
                affinity_failures.fetch_add(1, std::memory_order_relaxed);
            }
        };
        std::thread binance_thread([&] { pin_role(role_cpus[1]); binance.run(stop_token); });
        std::thread coinbase_thread([&] { pin_role(role_cpus[2]); coinbase.run(stop_token); });
        pm_feed.start();
        // Pin the owner only after child creation so a failed child pin cannot
        // accidentally inherit the decision owner's one-CPU mask.
        pin_role(role_cpus[0]);

        const auto deadline = start_mono + static_cast<std::int64_t>(options.duration_seconds) * 1'000'000'000LL;
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
        const auto refill_pm = [&] {
            while (!pm_ready && pm_queue.try_pop(pending_pm)) {
                const auto epoch = pm_epoch.load(std::memory_order_acquire);
                if (pending_pm.connection_epoch == epoch) pm_ready = true;
            }
        };
        while (monotonic_now_ns() < deadline) {
            if (pm_faults.exchange(0, std::memory_order_acq_rel) != 0) {
                yes_book.valid = 0; yes_book.lineage_continuous = 0;
                no_book.valid = 0; no_book.lineage_continuous = 0;
                if (pm_ready && pending_pm.connection_epoch != pm_epoch.load(std::memory_order_acquire)) {
                    pm_ready = false;
                }
            }
            refill_binance();
            refill_coinbase();
            refill_pm();
            if (!binance_ready && !coinbase_ready && !pm_ready) {
                (void)wakeup.wait_for(2ms, std::chrono::microseconds(options.idle_spin_us));
                continue;
            }
            std::int64_t receive_ns = std::numeric_limits<std::int64_t>::max();
            if (binance_ready) receive_ns = std::min(receive_ns, pending_binance.local_receive_monotonic_ns);
            if (coinbase_ready) receive_ns = std::min(receive_ns, pending_coinbase.local_receive_monotonic_ns);
            if (pm_ready) receive_ns = std::min(receive_ns, pending_pm.event.receive_monotonic_ns);
            if (receive_ns <= 0 || receive_ns == std::numeric_limits<std::int64_t>::max()) {
                ++latency_overflow;
                binance_ready = coinbase_ready = pm_ready = false;
                continue;
            }
            bool has_external = (binance_ready && pending_binance.local_receive_monotonic_ns == receive_ns)
                || (coinbase_ready && pending_coinbase.local_receive_monotonic_ns == receive_ns);
            if (has_external && receive_ns > 1) {
                current_signal = external_state.advance_external_cancel_signal(receive_ns - 1, external_policy);
            }
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
                if (pm_ready && pending_pm.event.receive_monotonic_ns == receive_ns) {
                    const auto& event = pending_pm.event;
                    if (event.instrument_handle == kYes) yes_book = event.book;
                    else if (event.instrument_handle == kNo) no_book = event.book;
                    pm_ready = false; refill_pm(); progressed = true;
                }
            } while (progressed);
            if (has_external) current_signal = external_state.advance_external_cancel_signal(receive_ns, external_policy);

            if (current_signal.signal_version != 0) {
                NativeCryptoDecisionInput input;
                input.signal = current_signal;
                input.market = market;
                input.yes_book = yes_book;
                input.no_book = no_book;
                input.now_monotonic_ns = receive_ns;
                const auto result = lane.evaluate(input, capital);
                const auto finished = monotonic_now_ns();
                ++evaluations;
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
                if (result.accepted != 0) {
                    ++accepted;
                    lane.mark_market_traded(kMarket);
                    if (accepted_signal_to_admission.size() < accepted_signal_to_admission.capacity()) {
                        accepted_signal_to_admission.push_back(std::max<std::int64_t>(
                            0, finished - current_signal.trigger_receive_monotonic_ns));
                    } else ++latency_overflow;
                }
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
        const auto total_affinity_failures = affinity_failures.load(std::memory_order_relaxed)
            + pm_status.affinity_errors;
        const auto rx_alignment_errors = binance_status.rx_alignment_errors
            + coinbase_status.rx_alignment_errors + pm_status.rx_alignment_errors;
        const auto rx_rejections = binance_status.rx_rejections
            + coinbase_status.rx_rejections + pm_status.rx_rejections;
        const bool rx_alignment_observed = !dynamic_rx_requested
            || (binance_status.incoming_cpu >= 0 && coinbase_status.incoming_cpu >= 0
                && pm_status.incoming_cpu >= 0);
        const bool rx_alignment_clean = !dynamic_rx_requested
            || (rx_alignment_observed && rx_alignment_errors == 0 && rx_rejections == 0);
        const bool clean = pm_drops.load() == 0
            && binance_ingress_status.dropped_events == 0
            && coinbase_ingress_status.dropped_events == 0
            && (!cpu_pin_active || total_affinity_failures == 0)
            && rx_alignment_clean;

        std::cout << json::serialize(json::object{
            {"schema", "polymarket_v7_crypto_flash_shadow_v1"},
            {"paper_only", true}, {"authenticated_execution", false},
            {"real_order_submission", false}, {"real_capital_at_risk", false},
            {"authority", "SHADOW_ZERO_AUTHORITY"},
            {"critical_path", "CPP_SAME_PROCESS_CAUSAL_EVENT_TO_RISK_ADMISSION"},
            {"clean_capture", clean}, {"duration_seconds", options.duration_seconds},
            {"idle_spin_us", options.idle_spin_us}, {"socket_busy_poll_us", options.socket_busy_poll_us},
            {"kernel_wakeups", wakeup.kernel_wakeups()},
            {"cpu_pin_requested", options.cpu_pin != 0}, {"cpu_pin_active", cpu_pin_active},
            {"dynamic_rx_align_requested", dynamic_rx_requested},
            {"rx_alignment_observed", rx_alignment_observed},
            {"rx_alignment_clean", rx_alignment_clean},
            {"rx_alignment_errors", rx_alignment_errors}, {"rx_rejections", rx_rejections},
            {"allowed_cpu_count", allowed_cpus.size()}, {"affinity_failures", total_affinity_failures},
            {"cpu_roles", {{"decision", role_cpus[0]}, {"binance", role_cpus[1]},
                           {"coinbase", role_cpus[2]}, {"polymarket", role_cpus[3]}}},
            {"evaluations", evaluations}, {"accepted_candidates", accepted},
            {"latency_sample_overflow", latency_overflow},
            {"accepted_signal_to_admission", latency_distribution(std::move(accepted_signal_to_admission))},
            {"first_signal_to_decision", latency_distribution(std::move(first_signal_to_decision))},
            {"decision_compute", latency_distribution(std::move(decision_compute))},
            {"reason_counts", reason_json(reasons)},
            {"binance", {{"frames", binance_status.frames_received}, {"transport_failures", binance_status.transport_failures}, {"drops", binance_ingress_status.dropped_events},
                         {"incoming_cpu", binance_status.incoming_cpu}, {"incoming_napi_id", binance_status.incoming_napi_id}, {"rx_realignments", binance_status.rx_realignments}}},
            {"coinbase", {{"frames", coinbase_status.frames_received}, {"transport_failures", coinbase_status.transport_failures}, {"drops", coinbase_ingress_status.dropped_events},
                          {"incoming_cpu", coinbase_status.incoming_cpu}, {"incoming_napi_id", coinbase_status.incoming_napi_id}, {"rx_realignments", coinbase_status.rx_realignments}}},
            {"polymarket", {{"messages", pm_status.messages}, {"reconnects", pm_status.reconnects},
                            {"errors", pm_status.errors}, {"affinity_errors", pm_status.affinity_errors}, {"drops", pm_drops.load()},
                            {"incoming_cpu", pm_status.incoming_cpu}, {"incoming_napi_id", pm_status.incoming_napi_id}, {"rx_realignments", pm_status.rx_realignments}}},
            {"note", "No order adapter is instantiated. Accepted candidates are shadow admissions only."}
        }) << '\n';
        return clean ? 0 : 2;
    } catch (const std::exception& error) {
        std::cerr << "crypto_flash_shadow: " << error.what() << '\n';
        return 64;
    }
}
