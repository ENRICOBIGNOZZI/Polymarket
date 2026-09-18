#include "pm/v7_native_settlement_oms_endpoint.hpp"
#include "pm/v7_native_paper_execution.hpp"
#include "pm/v7_native_runtime_evidence.hpp"
#include "pm/v7_native_runtime_evidence.hpp"
#include "pm/fast_ws.hpp"
#include "pm/v7_coinbase_l2_observer.hpp"
#include "pm/v7_crypto_decision_lane.hpp"
#include "pm/v7_external_ingress.hpp"
#include "pm/v7_external_ws.hpp"
#include "pm/v7_ingress_wakeup.hpp"
#include "pm/v7_market_ws.hpp"
#include "pm/v7_maker_lane.hpp"
#include "pm/v7_native_settlement_authority.hpp"
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
    std::string yes_token;
    std::string no_token;
    std::string run_root;
    std::string model_sha;
    std::string run_id;
    std::string server_id;
    std::string market_id;
    std::string event_id;
    std::string fee_source;
    std::string pm_ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/market";
    std::int64_t close_wall_ns = 0;
    std::int32_t tick_size_e4 = 100;
    std::int64_t min_order_microunits = 5'000'000;
    double taker_fee_rate = 0.0;
    double taker_fee_exponent = 1.0;
    int duration_seconds = 0;
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
        else if (arg == "--run-root") out.run_root = next();
        else if (arg == "--model-sha") out.model_sha = next();
        else if (arg == "--run-id") out.run_id = next();
        else if (arg == "--server-id") out.server_id = next();
        else if (arg == "--market-id") out.market_id = next();
        else if (arg == "--event-id") out.event_id = next();
        else if (arg == "--fee-source") out.fee_source = next();
        else if (arg == "--pm-ws-url") out.pm_ws_url = next();
        else if (arg == "--close-wall-ns") out.close_wall_ns = bounded_integer<std::int64_t>(next(), 1, std::numeric_limits<std::int64_t>::max());
        else if (arg == "--tick-size-e4") out.tick_size_e4 = bounded_integer<std::int32_t>(next(), 1, 5000);
        else if (arg == "--min-order-microunits") out.min_order_microunits = bounded_integer<std::int64_t>(next(), 1, 1'000'000'000);
        else if (arg == "--taker-fee-rate") out.taker_fee_rate = bounded_double(next(), 0.0, 1.0);
        else if (arg == "--taker-fee-exponent") out.taker_fee_exponent = bounded_double(next(), 0.0, 10.0);
        else if (arg == "--duration-seconds") out.duration_seconds = bounded_integer<int>(next(), 0, 86'400);
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
            std::cout << "native crypto settlement candidate configuration PASS\n";
            return 0;
        }
        if (options.yes_token.empty() || options.no_token.empty()
            || options.yes_token == options.no_token || options.run_root.empty()
            || !exact_sha(options.model_sha) || options.run_id.empty()
            || options.server_id.empty() || options.market_id.empty()
            || options.event_id.empty() || options.fee_source.empty()
            || options.close_wall_ns <= wall_now_ns()) {
            throw std::invalid_argument("live PAPER runtime identity required");
        }

        constexpr std::uint64_t kAsset = 1, kMarket = 1, kEvent = 1, kYes = 1, kNo = 2;
        IngressWakeup wakeup;
        ExternalVenueIngress binance_ingress(VenueId::BinanceSpot, kAsset, nullptr, &wakeup);
        ExternalVenueIngress coinbase_ingress(VenueId::CoinbaseSpot, kAsset, nullptr, &wakeup);
        auto binance_spec = btc_spot_connection_spec(VenueId::BinanceSpot, kAsset);
        auto coinbase_spec = btc_spot_connection_spec(VenueId::CoinbaseSpot, kAsset);
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
        NativeSettlementAuthority authority(limits);
        NativeSettlementOmsEndpoint adapter_endpoint(authority);
        NativePaperExecutionAdapter paper_execution(adapter_endpoint);
        NativeRuntimeEvidenceConfig evidence_config{};
        evidence_config.run_root = options.run_root;
        evidence_config.model_sha = options.model_sha;
        evidence_config.run_id = options.run_id;
        evidence_config.server_id = options.server_id;
        evidence_config.market_id = options.market_id;
        evidence_config.event_id = options.event_id;
        evidence_config.yes_token_id = options.yes_token;
        evidence_config.no_token_id = options.no_token;
        evidence_config.fee_source = options.fee_source;
        evidence_config.yes_instrument_handle = kYes;
        evidence_config.no_instrument_handle = kNo;
        evidence_config.close_wall_ns = options.close_wall_ns;
        evidence_config.taker_fee_rate = options.taker_fee_rate;
        evidence_config.taker_fee_exponent = options.taker_fee_exponent;
        evidence_config.taker_only_fee = 1;
        NativeRuntimeEvidenceWriter evidence_writer(evidence_config);
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
        maker::MakerLaneContext maker_context;
        maker_context.risk.max_quote_shares = maker_model.base_quote_shares;
        maker_context.risk.max_abs_residual_shares = maker_model.base_quote_shares;

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
        std::vector<std::int64_t> accepted_signal_to_adapter;
        std::vector<std::int64_t> first_signal_to_decision;
        std::vector<std::int64_t> decision_compute;
        accepted_signal_to_admission.reserve(4096);
        accepted_signal_to_adapter.reserve(4096);
        first_signal_to_decision.reserve(4096);
        decision_compute.reserve(4096);
        std::array<std::uint64_t, 32> reasons{};
        std::uint64_t evaluations = 0, accepted = 0, latency_overflow = 0;
        std::uint64_t taker_accepted = 0, maker_accepted = 0;
        std::uint64_t adapter_handoff_failures = 0;
        std::uint64_t maker_decisions = 0, maker_candidates = 0;
        std::uint64_t maker_cancel_intents = 0, maker_cancel_handoffs = 0;
        std::uint64_t maker_cancel_not_ready = 0, maker_duplicate_quotes = 0;
        std::uint64_t maker_replace_pending = 0;
        std::uint64_t paper_trade_sequence = 0, paper_fill_events = 0;
        std::uint64_t paper_invalid_trades = 0, paper_submit_failures = 0;
        std::uint64_t arbitration_conflicts = 0, authority_rejections = 0;
        std::uint64_t inventory_rejections = 0, minimum_size_rejections = 0;
        std::uint64_t last_measured_signal_version = 0;
        const auto publish_order = [&](const NativeOrderCommand& command,
                                       ExecutionPolicyId policy,
                                       std::int64_t exchange_event_ns,
                                       std::int64_t receive_monotonic_ns) noexcept {
            NativeEvidenceEvent evidence{};
            evidence.kind = NativeEvidenceKind::OrderSubmitted;
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
                                       OrderState state) noexcept {
            NativeEvidenceEvent evidence{};
            evidence.kind = NativeEvidenceKind::OrderState;
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
                (void)wakeup.wait_for(2ms);
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
                || (coinbase_ready && pending_coinbase.local_receive_monotonic_ns == receive_ns);
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
                if (pm_ready && pending_pm.event.receive_monotonic_ns == receive_ns) {
                    const auto& event = pending_pm.event;
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
                    if (event.instrument_handle == kYes) {
                        yes_book = event.book;
                        maker_decision = yes_maker.on_market_event(event, maker_context, maker_model);
                        maker_event = true;
                    } else if (event.instrument_handle == kNo) {
                        no_book = event.book;
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

            if (current_signal.signal_version != 0) {
                NativeCryptoDecisionInput input;
                input.signal = current_signal;
                input.market = market;
                input.yes_book = yes_book;
                input.no_book = no_book;
                input.now_monotonic_ns = receive_ns;
                const auto result = lane.construct_candidate(input);
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
                    ExecutionPlan plan;
                    plan.intent = result.intent;
                    plan.tick_size_e4 = (result.selected_yes != 0 ? yes_book : no_book).tick_size_e4;
                    plan.market_state_version = result.intent.state_version;
                    plan.policy = ExecutionPolicyId::AggressiveTaker;
                    append_candidate(plan, current_signal.trigger_receive_monotonic_ns, true);
                }
            }

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
                    if (paper_result.final_state == OrderState::Rejected
                        || paper_result.final_state == OrderState::Expired) {
                        if (!publish_state(authority_result.tx.command,
                                           alpha_candidates[index].policy,
                                           paper_result.final_state)) {
                            ++adapter_handoff_failures;
                            break;
                        }
                    }
                    ++accepted;
                    if (paper_result.accepted != 0 && paper_result.filled_microunits > 0) {
                        ++paper_fill_events;
                        if (!publish_fill(paper_result.fill,
                                          alpha_candidates[index].policy)
                            || !publish_state(authority_result.tx.command,
                                              alpha_candidates[index].policy,
                                              paper_result.fill.order_state)) {
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

        pm_feed.stop();
#if defined(__APPLE__)
        stopping.store(true, std::memory_order_release);
#else
        stopping.request_stop();
#endif
        binance_thread.join();
        coinbase_thread.join();
        evidence_writer.stop();

        const auto binance_status = binance.snapshot();
        const auto coinbase_status = coinbase.snapshot();
        const auto pm_status = pm_feed.snapshot();
        const auto binance_ingress_status = binance_ingress.snapshot();
        const auto coinbase_ingress_status = coinbase_ingress.snapshot();
        const bool clean = pm_drops.load() == 0
            && binance_ingress_status.dropped_events == 0
            && coinbase_ingress_status.dropped_events == 0
            && adapter_handoff_failures == 0
            && evidence_writer.healthy()
            && evidence_writer.dropped() == 0
            && evidence_writer.published() == evidence_writer.written();

        std::cout << json::serialize(json::object{
            {"schema", "polymarket_v7_crypto_settlement_native_candidate_v2"},
            {"paper_only", true}, {"authenticated_execution", false},
            {"real_order_submission", false}, {"real_capital_at_risk", false},
            {"authority", "PAPER_SIMULATED_SINGLE_OWNER"},
            {"critical_path", "CPP_SAME_PROCESS_FEED_DECODE_TO_SINGLE_SETTLEMENT_AUTHORITY"},
            {"clean_capture", clean}, {"duration_seconds", options.duration_seconds},
            {"evaluations", evaluations}, {"accepted_candidates", accepted},
            {"taker_accepted", taker_accepted}, {"maker_accepted", maker_accepted},
            {"maker_decisions", maker_decisions}, {"maker_candidates", maker_candidates},
            {"maker_cancel_intents", maker_cancel_intents},
            {"maker_cancel_handoffs", maker_cancel_handoffs},
            {"maker_cancel_not_ready", maker_cancel_not_ready},
            {"maker_duplicate_quotes", maker_duplicate_quotes},
            {"maker_replace_pending", maker_replace_pending},
            {"paper_resting_orders", paper_execution.resting_orders()},
            {"paper_synthetic_acks", paper_execution.synthetic_acks()},
            {"paper_fill_events", paper_fill_events},
            {"paper_adapter_fills", paper_execution.paper_fills()},
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
            {"adapter_handoff_failures", adapter_handoff_failures},
            {"native_oms_active_orders", authority.active_orders()},
            {"adapter_unsent_observations", adapter_endpoint.observed_unsent()},
            {"adapter_healthy", adapter_endpoint.healthy()},
            {"network_orders_sent", 0},
            {"simulated_fills", paper_execution.paper_fills()},
            {"latency_sample_overflow", latency_overflow},
            {"accepted_signal_to_admission", latency_distribution(std::move(accepted_signal_to_admission))},
            {"accepted_signal_to_adapter", latency_distribution(std::move(accepted_signal_to_adapter))},
            {"first_signal_to_decision", latency_distribution(std::move(first_signal_to_decision))},
            {"decision_compute", latency_distribution(std::move(decision_compute))},
            {"reason_counts", reason_json(reasons)},
            {"binance", {{"frames", binance_status.frames_received}, {"transport_failures", binance_status.transport_failures}, {"drops", binance_ingress_status.dropped_events}}},
            {"coinbase", {{"frames", coinbase_status.frames_received}, {"transport_failures", coinbase_status.transport_failures}, {"drops", coinbase_ingress_status.dropped_events}}},
            {"polymarket", {{"messages", pm_status.messages}, {"reconnects", pm_status.reconnects}, {"errors", pm_status.errors}, {"drops", pm_drops.load()}}},
            {"note", "PAPER-only native candidate. Maker and taker share one in-process inventory/capital/OMS authority. Taker fills require causal executable L1 depth; maker fills use pessimistic public-print queue depletion and bounded cancel latency. No authenticated submission or real capital is possible."}
        }) << '\n';
        return clean ? 0 : 2;
    } catch (const std::exception& error) {
        std::cerr << "crypto_settlement_native_candidate: " << error.what() << '\n';
        return 64;
    }
}
