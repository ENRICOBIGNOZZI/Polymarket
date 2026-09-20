#include "pm/v7_native_runtime_evidence.hpp"

#include <boost/json.hpp>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <limits>
#include <sstream>
#include <string_view>
#include <thread>

namespace pm::v7 {
namespace fs = std::filesystem;
namespace json = boost::json;

json::object slow_context_json(const SlowContextCut& cut) {
    json::object fields;
    for (std::size_t i = 0; i < kSlowContextFields; ++i) {
        const auto& field = cut.snapshot.fields[i];
        if ((cut.fresh_mask & (1U << i)) == 0) {
            fields[std::string(kSlowContextNames[i])] = nullptr;
        } else {
            fields[std::string(kSlowContextNames[i])] = json::object{
                {"value", field.value}, {"receive_monotonic_ns", field.receive_ns},
                {"expires_monotonic_ns", field.expires_ns}, {"source_version", field.source_version}};
        }
    }
    return {{"schema", "polymarket_v7_slow_context_cut_v1"},
        {"version", cut.snapshot.version}, {"fresh_mask", cut.fresh_mask},
        {"decision_monotonic_ns", cut.decision_ns},
        {"max_input_receive_monotonic_ns", cut.max_input_receive_ns},
        {"model_used_mask", 0}, {"fields", std::move(fields)}};
}

namespace {

[[nodiscard]] bool exact_sha(const std::string& value) noexcept {
    if (value.size() != 40) return false;
    for (const char ch : value) {
        if (!((ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f'))) return false;
    }
    return true;
}

[[nodiscard]] bool exact_hex16(const std::string& value) noexcept {
    if (value.size() != 16) return false;
    for (const char ch : value) {
        if (!((ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f'))) return false;
    }
    return true;
}

[[nodiscard]] bool exact_hex64(const std::string& value) noexcept {
    if (value.size() != 64) return false;
    for (const char ch : value) {
        if (!((ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f'))) return false;
    }
    return true;
}

constexpr std::string_view kMakerExecutionSemantics =
    "maker-paper-v7.2-bilateral-inventory";

[[nodiscard]] std::int64_t wall_now_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}

[[nodiscard]] std::int64_t monotonic_now_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

[[nodiscard]] std::int64_t to_ms(std::int64_t ns) noexcept {
    return ns > 0 ? ns / 1'000'000LL : 0;
}

[[nodiscard]] const char* side_name(Side side) noexcept {
    return side == Side::Buy ? "BUY" : side == Side::Sell ? "SELL" : "";
}

[[nodiscard]] const char* state_name(OrderState state) noexcept {
    switch (state) {
        case OrderState::Live: return "LIVE";
        case OrderState::Partial: return "PARTIAL";
        case OrderState::Filled: return "FILLED";
        case OrderState::Cancelled: return "CANCELLED";
        case OrderState::Rejected: return "REJECTED";
        case OrderState::Expired: return "EXPIRED";
        case OrderState::Unknown: return "UNKNOWN";
        default: return "PENDING";
    }
}

[[nodiscard]] const char* strategy_component(StrategyId strategy) noexcept {
    return strategy == StrategyId::ProfessionalMaker
        ? "professional_maker" : "crypto_informed_taker";
}

[[nodiscard]] const char* native_decision_reason_name(std::uint8_t reason) noexcept {
    switch (reason) {
    case 1: return "ACCEPTED";
    case 2: return "INVALID_SIGNAL";
    case 3: return "EXPIRED_SIGNAL";
    case 4: return "WEAK_SIGNAL";
    case 5: return "MARKET_UNAVAILABLE";
    case 6: return "TTE_OUTSIDE_WINDOW";
    case 7: return "INVALID_BOOK";
    case 8: return "INSUFFICIENT_DEPTH";
    case 9: return "INVALID_TICK";
    case 10: return "DUPLICATE_SIGNAL";
    case 11: return "MARKET_ALREADY_TRADED";
    case 12: return "CAPITAL_DENIED";
    case 13: return "ENTRY_PRICE_TOO_HIGH";
    case 14: return "MARKET_ALREADY_REPRICED";
    case 15: return "PROBABILITY_UNAVAILABLE";
    case 16: return "NET_EDGE_NON_POSITIVE";
    case 17: return "RISK_SIZE_BELOW_MINIMUM";
    case 18: return "SLOW_CONTEXT_UNAVAILABLE";
    default: return "UNKNOWN";
    }
}

void atomic_write(const fs::path& target, const std::string& payload) {
    fs::create_directories(target.parent_path());
    const fs::path temporary = target.string() + ".tmp";
    {
        std::ofstream out(temporary, std::ios::binary | std::ios::trunc);
        if (!out) throw std::runtime_error("native evidence temporary open failed");
        out.write(payload.data(), static_cast<std::streamsize>(payload.size()));
        out.flush();
        if (!out) throw std::runtime_error("native evidence temporary write failed");
    }
    fs::rename(temporary, target);
}

} // namespace

bool NativeRuntimeEvidenceConfig::valid() const noexcept {
    return !run_root.empty() && exact_sha(model_sha) && !run_id.empty()
        && !server_id.empty() && !asset.empty() && !horizon.empty()
        && !market_id.empty() && !event_id.empty()
        && !yes_token_id.empty() && !no_token_id.empty()
        && !fee_source.empty() && exact_hex16(maker_execution_policy_hash)
        && exact_hex16(maker_execution_config_hash)
        && maker_execution_semantics == kMakerExecutionSemantics
        && yes_instrument_handle != 0
        && no_instrument_handle != 0 && yes_instrument_handle != no_instrument_handle
        && close_wall_ns > 0 && std::isfinite(taker_fee_rate) && taker_fee_rate >= 0.0
        && std::isfinite(taker_fee_exponent) && taker_fee_exponent >= 0.0
        && taker_maximum_entry_price_e4 > 0 && taker_maximum_entry_price_e4 <= 10'000
        && (signal_policy_sha256.empty() || exact_hex64(signal_policy_sha256))
        && (observation_capture_mode == "NONE"
            || observation_capture_mode == "DECISIONS"
            || observation_capture_mode == "DECISION_WINDOWS"
            || observation_capture_mode == "FULL");
}

struct NativeRuntimeEvidenceWriter::Impl {
    NativeRuntimeEvidenceConfig config;
    NativeRuntimeEvidenceWriter& owner;
    std::thread thread;
    std::int64_t wall_minus_monotonic_ns = 0;
    std::uint64_t sequence = 0, observation_sequence = 0;
    std::ofstream observations_file;
    fs::path observations_path;
    std::int64_t observations_watermark_ns = 0;
    std::array<std::uint64_t, 32> decision_reason_counts{};
    std::uint64_t decision_observations = 0;
    std::uint64_t accepted_decision_observations = 0;
    const std::string capture_id = std::to_string(monotonic_now_ns());

    Impl(NativeRuntimeEvidenceConfig value, NativeRuntimeEvidenceWriter& source)
        : config(std::move(value)), owner(source),
          wall_minus_monotonic_ns(wall_now_ns() - monotonic_now_ns()) {
        if (!config.valid()) throw std::invalid_argument("invalid native evidence config");
        thread = std::thread([this] { run(); });
    }

    [[nodiscard]] std::string token(std::uint64_t handle) const {
        if (handle == config.yes_instrument_handle) return config.yes_token_id;
        if (handle == config.no_instrument_handle) return config.no_token_id;
        return {};
    }

    [[nodiscard]] std::int64_t wall_ns_from_monotonic(std::int64_t ns) const noexcept {
        if (ns <= 0 || wall_minus_monotonic_ns <= 0
            || ns > std::numeric_limits<std::int64_t>::max() - wall_minus_monotonic_ns) return 0;
        return ns + wall_minus_monotonic_ns;
    }

    [[nodiscard]] std::int64_t wall_ms_from_monotonic(std::int64_t ns) const noexcept {
        return to_ms(wall_ns_from_monotonic(ns));
    }

    [[nodiscard]] json::object receipt(const NativeOrderCommand& command) const {
        return {
            {"schema", "polymarket_v7_native_settlement_receipt_v1"},
            {"owner", "V7_NATIVE_CRYPTO_SETTLEMENT_ENGINE"},
            {"engine_id", "CRYPTO_SETTLEMENT_ENGINE"},
            {"model_sha", config.model_sha},
            {"asset", config.asset},
            {"horizon", config.horizon},
            {"paper_only", true},
            {"authenticated_execution", false},
            {"real_order_submission", false},
            {"real_capital_at_risk", false},
            {"execution_mode", "PAPER_SIMULATED"},
            {"paper_simulation_authority", true},
            {"real_new_risk_authorized", false},
            {"single_owner", true},
            {"owner_chain", json::array{"portfolio", "risk", "capital", "oms", "inventory"}},
            {"order_namespace", config.run_id + ":" + config.market_id},
            {"client_order_id", command.client_order_id},
            {"command_id", command.command_id},
        };
    }

    [[nodiscard]] double fee(const NativePaperFillRecord& fill) const noexcept {
        if (fill.fill_microunits <= 0 || fill.tick_size_e4 <= 0 || fill.price_tick <= 0) return 0.0;
        if (fill.taker == 0 && config.taker_only_fee != 0) return 0.0;
        const double price = static_cast<double>(fill.price_tick)
            * static_cast<double>(fill.tick_size_e4) / 10'000.0;
        if (!(price > 0.0 && price < 1.0)) return 0.0;
        const double per_share = config.taker_fee_rate
            * std::pow(price * (1.0 - price), config.taker_fee_exponent);
        return static_cast<double>(fill.fill_microunits) / 1'000'000.0 * per_share;
    }

    [[nodiscard]] json::object base(const NativeEvidenceEvent& event, std::string event_type) {
        ++sequence;
        std::ostringstream ordinal;
        ordinal << std::setw(20) << std::setfill('0') << sequence;
        const auto recorded_ms = to_ms(wall_now_ns());
        const auto order_id = std::string("native:") + config.run_id + ":" + config.market_id
            + ":" + std::to_string(event.command.client_order_id);
        const auto component = strategy_component(event.strategy_id);
        const bool maker = event.strategy_id == StrategyId::ProfessionalMaker;
        json::object metadata{
            {"component", component},
            {"crypto_context", json::object{{"asset", config.asset}, {"horizon", config.horizon}}},
            {"code_sha", config.model_sha},
            {"model_artifact_hash", event.strategy_id == StrategyId::ProfessionalMaker && !config.maker_artifact_sha256.empty()
                ? json::value(config.maker_artifact_sha256) : json::value(nullptr)},
            {"maker_policy_sha256", config.maker_policy_sha256.empty() ? json::value(nullptr) : json::value(config.maker_policy_sha256)},
            {"policy_hash", maker ? json::value(config.maker_execution_policy_hash) : json::value(nullptr)},
            {"config_hash", maker ? json::value(config.maker_execution_config_hash) : json::value(nullptr)},
            {"execution_semantics_version", maker ? json::value(config.maker_execution_semantics) : json::value(nullptr)},
            {"identity_provenance", maker ? json::value("EXACT_RUNTIME_ARTIFACT_V1") : json::value(nullptr)},
            {"prediction_model_kind", event.strategy_id == StrategyId::ProfessionalMaker
                ? (config.maker_valid_cells > 0 ? "EXECUTION_CELLS_LOADED" : "DEFAULT_BASELINE")
                : config.probability_artifact_sha256.empty() ? "FROZEN_DIRECTIONAL_RULE"
                : "EXPERIMENTAL_SETTLEMENT_PROBABILITY_V1"},
            {"probability_artifact_sha256", config.probability_artifact_sha256.empty()
                ? json::value(nullptr) : json::value(config.probability_artifact_sha256)},
            {"probability_forward_calibrated", false},
            {"probability_evaluation_end_wall_ns",
                config.probability_evaluation_end_wall_ns > 0
                    ? json::value(config.probability_evaluation_end_wall_ns) : json::value(nullptr)},
            {"maker_valid_cells", config.maker_valid_cells},
            {"model_family", component},
            {"paper_exploration", true},
            {"causal_arrival_verified", false},
            {"exchange_execution_verified", false},
            {"taker_maximum_entry_price_e4", config.taker_maximum_entry_price_e4},
            {"paper_venue_delay_ns", config.paper_venue_delay_ns},
            {"paper_assumed_transport_delay_ns", config.paper_assumed_transport_delay_ns},
            {"paper_terms_sha256", config.paper_terms_sha256},
            {"signal_policy_sha256", config.signal_policy_sha256.empty()
                ? json::value(nullptr) : json::value(config.signal_policy_sha256)},
            {"paper_execution_reason", static_cast<unsigned>(event.paper_reason)},
            {"execution_observation_censored", event.paper_censored != 0},
            {"paper_simulator_semantics", maker ? "PUBLIC_PRINT_QUEUE_RESEARCH"
                : config.paper_venue_delay_ns < 0 ? "VENUE_TERMS_UNKNOWN_NO_TAKER_FILL"
                : "LOCAL_RECEIVE_DELAYED_ARRIVAL_PRICE_PARTIAL_FAK_V2"},
            {"economic_authority", "PAPER_EXPLORATION"},
            {"action_value_semantics", maker
                ? "MAKER_FILL_CONDITIONED_ROBUST_EV_PER_SHARE"
                : (event.probability.valid != 0 && event.economics.accepted != 0)
                    ? "TAKER_SETTLEMENT_EDGE_NOT_FILL_CONDITIONED"
                    : "TAKER_RULE_NO_COMPARABLE_ACTION_SCORE"},
            {"counterfactual", false},
            {"research_evidence_only", false},
            {"native_settlement_receipt", receipt(event.command)},
            {"run_id", config.run_id},
            {"server_id", config.server_id},
            {"asset", config.asset},
            {"horizon", config.horizon},
        };
        return {
            {"schema_version", 1},
            {"event_type", std::move(event_type)},
            {"strategy", "CRYPTO_SETTLEMENT_ENGINE"},
            {"model_sha", config.model_sha},
            {"paper_only", true},
            {"authenticated_execution", false},
            {"record_id", config.run_id + ":" + config.market_id + ":native:" + ordinal.str()},
            {"recorded_ts_ms", recorded_ms},
            {"model_version", "native-paper-engine"},
            {"order_id", order_id},
            {"market_id", config.market_id},
            {"event_id", config.event_id},
            {"token_id", token(event.command.instrument_handle)},
            {"side", side_name(event.command.side)},
            {"metadata", std::move(metadata)},
        };
    }

    [[nodiscard]] json::object encode(const NativeEvidenceEvent& event) {
        if (event.kind == NativeEvidenceKind::OrderSubmitted) {
            auto out = base(event, "ORDER_SUBMITTED");
            const auto receive_ms = wall_ms_from_monotonic(event.causal_receive_monotonic_ns);
            const auto decision_ms = wall_ms_from_monotonic(event.command.decision_monotonic_ns);
            out["exchange_ts_ms"] = to_ms(event.causal_exchange_event_ns);
            out["receive_ts_ms"] = receive_ms;
            out["decision_ts_ms"] = decision_ms;
            out["book_snapshot_id"] = "native-book:" + std::to_string(event.command.market_state_version);
            out["limit_price"] = static_cast<double>(event.command.price_tick)
                * static_cast<double>(event.command.tick_size_e4) / 10'000.0;
            out["intended_action"] = event.policy == ExecutionPolicyId::AggressiveTaker ? "TAKE" : "MAKE";
            out["intended_size"] = static_cast<double>(event.command.quantity_microunits) / 1'000'000.0;
            out["metadata"].as_object()["slow_context"] = slow_context_json(event.slow_context);
            if (event.probability.valid && event.economics.accepted) {
                out["predicted_alpha"] = event.economics.expected_net_edge;
                out["expected_ev"] = event.economics.expected_net_edge
                    * static_cast<double>(event.command.quantity_microunits) / 1'000'000.;
                auto& md = out["metadata"].as_object();
                md["probability_input_token_id"] = token(event.probability_input_instrument);
                json::array features;
                for (double x : event.probability_features) features.emplace_back(x);
                md["probability_input_features"] = std::move(features);
                md["probability_up"] = event.probability.up;
                md["probability_up_lower"] = event.probability.lower;
                md["probability_up_upper"] = event.probability.upper;
                md["selected_probability"] = event.economics.probability;
                md["selected_probability_lower"] = event.economics.probability_lower;
                md["net_edge_per_share"] = event.economics.expected_net_edge;
                md["conservative_net_edge_per_share"] = event.economics.conservative_net_edge;
                md["fee_estimate_per_share"] = event.economics.fee_per_share;
                md["all_in_cost_ceiling_per_share"] = event.economics.cost_per_share;
                md["decision_observed_ask"] =
                    static_cast<double>(event.economics.observed_ask_e4) / 10'000.0;
                md["maximum_executable_price"] =
                    static_cast<double>(event.economics.maximum_executable_price_e4) / 10'000.0;
                md["worst_case_all_in_cost_per_share"] =
                    event.economics.worst_case_cost_per_share;
                md["worst_case_conservative_net_edge_per_share"] =
                    event.economics.worst_case_conservative_net_edge;
                md["order_cost_ceiling_microdollars"] = event.economics.cost_ceiling_microdollars;
                md["uncertainty_semantics"] = "MODEL_PROXY_NOT_COVERAGE_CERTIFIED";
                md["execution_reserve_is_measured"] = false;
            }
            return out;
        }
        if (event.kind == NativeEvidenceKind::OrderState) {
            auto out = base(event, "ORDER_STATE");
            out["order_state"] = state_name(event.order_state);
            return out;
        }
        auto out = base(event, "FILL");
        auto& md = out.at("metadata").as_object();
        md["local_receive_arrival_modelled"] = event.fill.causal_arrival_modelled != 0;
        md["simulated_arrival_monotonic_ns"] = event.fill.receive_monotonic_ns;
        md["arrival_book_receive_monotonic_ns"] = event.fill.arrival_book_receive_ns;
        md["arrival_book_version"] = event.fill.arrival_book_version;
        const auto price = static_cast<double>(event.fill.price_tick)
            * static_cast<double>(event.fill.tick_size_e4) / 10'000.0;
        out["fill_id"] = "native:" + config.run_id + ":" + config.market_id
            + ":" + std::to_string(event.fill.client_order_id)
            + ":fill:" + std::to_string(sequence);
        out["position_id"] = "native-position:" + config.market_id + ":" + token(event.fill.instrument_handle);
        out["token_id"] = token(event.fill.instrument_handle);
        out["side"] = side_name(event.fill.side);
        out["exchange_ts_ms"] = to_ms(event.fill.exchange_event_ns);
        out["receive_ts_ms"] = wall_ms_from_monotonic(event.fill.receive_monotonic_ns);
        out["fill_price"] = price;
        out["filled_size"] = static_cast<double>(event.fill.fill_microunits) / 1'000'000.0;
        out["complete"] = event.order_state == OrderState::Filled;
        out["fee"] = fee(event.fill);
        out["fee_rate"] = event.fill.taker != 0 ? config.taker_fee_rate : 0.0;
        out["fee_source"] = config.fee_source;
        return out;
    }

    void write_event(const NativeEvidenceEvent& event) {
        auto value = encode(event);
        const auto record_id = std::string(value["record_id"].as_string());
        const fs::path directory = fs::path(config.run_root) / "ledger" / "spool";
        atomic_write(directory / (record_id + ".json"),
                     json::serialize(value) + "\n");
        owner.written_.fetch_add(1, std::memory_order_release);
    }

    void write_observation(const NativeObservation& event) {
        if (event.kind == 2) {
            ++decision_observations;
            if (event.accepted != 0) ++accepted_decision_observations;
            const auto index = static_cast<std::size_t>(event.reason);
            if (index < decision_reason_counts.size()) ++decision_reason_counts[index];
        }
        if (!observations_file.is_open()) {
            const auto directory = fs::path(config.run_root) / "research/native_observations" / config.run_id;
            fs::create_directories(directory);
            observations_path = directory / (config.market_id + "-" + capture_id + ".jsonl");
            observations_file.open(observations_path, std::ios::app);
            if (!observations_file) throw std::runtime_error("native observations open failed");
        }
        json::array bids, asks, probability_inputs;
        if (event.probability.valid)
            for (double x : event.probability_features) probability_inputs.emplace_back(x);
        for (std::size_t i = 0; i < 10; ++i) {
            if (event.bid_prices[i] > 0) bids.emplace_back(json::array{event.bid_prices[i], event.bid_quantities[i]});
            if (event.ask_prices[i] > 0) asks.emplace_back(json::array{event.ask_prices[i], event.ask_quantities[i]});
        }
        json::object value{
            {"schema", "polymarket_v7_native_observation_v1"},
            {"paper_only", true}, {"execution_authority", false},
            {"code_sha", config.model_sha},
            {"model_artifact_hash", event.kind == 4 && !config.maker_artifact_sha256.empty()
                ? json::value(config.maker_artifact_sha256) : json::value(nullptr)},
            {"maker_artifact_sha256", config.maker_artifact_sha256.empty() ? json::value(nullptr) : json::value(config.maker_artifact_sha256)},
            {"maker_policy_sha256", config.maker_policy_sha256.empty() ? json::value(nullptr) : json::value(config.maker_policy_sha256)},
            {"policy_hash", event.kind == 4 ? json::value(config.maker_execution_policy_hash) : json::value(nullptr)},
            {"config_hash", event.kind == 4 ? json::value(config.maker_execution_config_hash) : json::value(nullptr)},
            {"execution_semantics_version", event.kind == 4 ? json::value(config.maker_execution_semantics) : json::value(nullptr)},
            {"identity_provenance", event.kind == 4 ? json::value("EXACT_RUNTIME_ARTIFACT_V1") : json::value(nullptr)},
            {"run_id", config.run_id}, {"server_id", config.server_id},
            {"market_id", config.market_id}, {"token_id", token(event.instrument_handle)},
            {"asset", config.asset}, {"horizon", config.horizon},
            {"capture_id", capture_id}, {"connection_epoch", event.connection_epoch},
            {"capture_mode", config.observation_capture_mode},
            {"capture_semantics_version", 2},
            {"taker_maximum_entry_price_e4", config.taker_maximum_entry_price_e4},
            {"paper_venue_delay_ns", config.paper_venue_delay_ns},
            {"paper_assumed_transport_delay_ns", config.paper_assumed_transport_delay_ns},
            {"paper_terms_sha256", config.paper_terms_sha256},
            {"sequence", ++observation_sequence}, {"kind", event.kind},
            {"event_receive_monotonic_ns", event.event_receive_ns}, {"event_exchange_ns", event.event_exchange_ns},
            {"native_event_kind", event.event_kind},
            {"minimum_order_microunits", config.minimum_order_microunits},
            {"risk_policy_sha256", config.risk_policy_sha256}, {"fee_source", config.fee_source},
            {"receive_monotonic_ns", event.receive_ns}, {"exchange_event_ns", event.exchange_ns},
            {"observed_monotonic_ns", event.observed_ns}, {"trigger_monotonic_ns", event.trigger_ns},
            {"evaluated_grid_monotonic_ns", event.evaluated_grid_ns},
            {"valid_until_monotonic_ns", event.valid_until_ns},
            {"decision_monotonic_ns", event.decision_ns}, {"close_monotonic_ns", event.close_ns},
            {"trigger_wall_ns", wall_ns_from_monotonic(event.trigger_ns)},
            {"decision_wall_ns", wall_ns_from_monotonic(event.decision_ns)},
            {"close_wall_ns", wall_ns_from_monotonic(event.close_ns)},
            {"signal_age_ns", event.decision_ns > 0 && event.trigger_ns > 0
                ? json::value(std::max<std::int64_t>(0, event.decision_ns - event.trigger_ns))
                : json::value(nullptr)},
            {"tte_ns", event.decision_ns > 0 && event.close_ns > 0
                ? json::value(std::max<std::int64_t>(0, event.close_ns - event.decision_ns))
                : json::value(nullptr)},
            {"receive_wall_ms", wall_ms_from_monotonic(event.receive_ns)},
            {"signal_version", event.signal_version}, {"book_version", event.book_version},
            {"signal_return_bp", std::isfinite(event.signal_return_bp) ? json::value(event.signal_return_bp) : json::value(nullptr)},
            {"binance_return_100ms_bp", std::isfinite(event.binance_return_100ms_bp)
                ? json::value(event.binance_return_100ms_bp) : json::value(nullptr)},
            {"coinbase_return_100ms_bp", event.confirmation_venue != external_fair::VenueId::BybitSpot
                && std::isfinite(event.coinbase_return_100ms_bp)
                ? json::value(event.coinbase_return_100ms_bp) : json::value(nullptr)},
            {"confirmation_return_100ms_bp", event.confirmation_venue != external_fair::VenueId::Unknown
                && std::isfinite(event.confirmation_return_100ms_bp)
                ? json::value(event.confirmation_return_100ms_bp) : json::value(nullptr)},
            {"confirmation_venue",
                event.confirmation_venue == external_fair::VenueId::BybitSpot ? "BYBIT"
                : event.confirmation_venue == external_fair::VenueId::CoinbaseSpot ? "COINBASE"
                : "UNKNOWN"},
            {"direction", event.direction},
            {"confirmed_non_opposing", event.confirmed_non_opposing != 0},
            {"signal_valid", event.signal_valid != 0},
            {"reason", event.reason}, {"accepted", event.accepted != 0}, {"book_valid", event.valid != 0},
            {"bid_e4", event.bid_e4}, {"ask_e4", event.ask_e4}, {"tick_e4", event.tick_e4},
            {"bid_quantity", event.bid_quantity}, {"ask_quantity", event.ask_quantity},
            {"repricing_pair_valid", event.repricing_pair_valid != 0},
            {"yes_bid_e4", event.repricing_pair_valid ? json::value(event.yes_bid_e4) : json::value(nullptr)},
            {"yes_ask_e4", event.repricing_pair_valid ? json::value(event.yes_ask_e4) : json::value(nullptr)},
            {"no_bid_e4", event.repricing_pair_valid ? json::value(event.no_bid_e4) : json::value(nullptr)},
            {"no_ask_e4", event.repricing_pair_valid ? json::value(event.no_ask_e4) : json::value(nullptr)},
            {"repricing_origin_signal_version", event.repricing_origin_signal_version > 0
                ? json::value(event.repricing_origin_signal_version) : json::value(nullptr)},
            {"repricing_horizon_ms", event.repricing_horizon_ms > 0
                ? json::value(event.repricing_horizon_ms) : json::value(nullptr)},
            {"bids", std::move(bids)}, {"asks", std::move(asks)},
            {"trade_side", event.trade_side}, {"trade_e4", event.trade_e4}, {"trade_quantity", event.trade_quantity},
            {"fee_rate", config.taker_fee_rate}, {"fee_exponent", config.taker_fee_exponent},
            {"proposed_quantity", event.proposed_quantity}, {"proposed_price_tick", event.proposed_price_tick},
            {"ev_uncertainty", event.kind == 4 ? json::value(event.ev_uncertainty) : json::value(nullptr)},
            {"probability_forecast", event.probability.valid ? json::value(event.probability.up) : json::value(nullptr)},
            {"probability_up_lower", event.probability.valid ? json::value(event.probability.lower) : json::value(nullptr)},
            {"probability_up_upper", event.probability.valid ? json::value(event.probability.upper) : json::value(nullptr)},
            {"probability_artifact_sha256", config.probability_artifact_sha256.empty()
                ? json::value(nullptr) : json::value(config.probability_artifact_sha256)},
            {"probability_forward_calibrated", false},
            {"probability_input_token_id", event.probability.valid ? json::value(token(event.probability_input_instrument)) : json::value(nullptr)},
            {"slow_context", event.slow_context.decision_ns > 0
                ? json::value(slow_context_json(event.slow_context)) : json::value(nullptr)},
            {"probability_input_features", event.probability.valid ? json::value(std::move(probability_inputs)) : json::value(nullptr)},
            {"expected_net_edge", event.kind == 4 ? json::value(event.expected_ev)
                : std::isfinite(event.economics.expected_net_edge) ? json::value(event.economics.expected_net_edge) : json::value(nullptr)},
            {"conservative_net_edge", std::isfinite(event.economics.conservative_net_edge)
                ? json::value(event.economics.conservative_net_edge) : json::value(nullptr)},
            {"expected_fill_probability", event.expected_fill_probability_valid
                && std::isfinite(event.expected_fill_probability)
                    ? json::value(event.expected_fill_probability) : json::value(nullptr)},
            {"economic_score_fill_conditioned", event.economic_score_fill_conditioned != 0},
            {"selector_score_comparable", event.economic_score_fill_conditioned != 0
                && event.expected_fill_probability_valid != 0},
            {"probability_decision_reason", event.probability.valid
                ? json::value(static_cast<unsigned>(event.economics.reason)) : json::value(nullptr)},
            {"external_features", event.external_valid ? json::value(json::object{
                {"state_version", event.external_state_version},
                {"input_receive_ns", event.external_input_receive_ns},
                {"composite_price", event.external_composite_price},
                {"return_250ms", event.external_return_250ms_valid ? json::value(event.external_return_250ms) : json::value(nullptr)},
                {"return_1s", event.external_return_1s_valid ? json::value(event.external_return_1s) : json::value(nullptr)},
                {"return_5s", event.external_return_5s_valid ? json::value(event.external_return_5s) : json::value(nullptr)},
                {"native_vol_fast", event.external_vol_fast}, {"native_vol_slow", event.external_vol_slow},
                {"dispersion_bps", event.external_dispersion_bps}, {"fresh_venues", event.external_fresh_venues},
                {"oracle_basis", nullptr}, {"opening_reference", nullptr},
                {"volatility_units", "EVENT_TIME_LOG_RETURN_RMS_NOT_PER_SECOND"},
                {"long_horizon_history_validated", false},
                {"source", "NATIVE_DIAGNOSTIC_NOT_STRUCTURAL_SETTLEMENT_MODEL"}}) : json::value(nullptr)},
        };
        observations_file << json::serialize(value) << '\n';
        if (!observations_file) throw std::runtime_error("native observations write failed");
        observations_watermark_ns = std::max(observations_watermark_ns, event.observed_ns);
        owner.observations_written_.fetch_add(1, std::memory_order_release);
    }

    void write_status() {
        json::object reason_counts;
        for (std::size_t i = 0; i < decision_reason_counts.size(); ++i) {
            if (decision_reason_counts[i] == 0) continue;
            reason_counts[native_decision_reason_name(static_cast<std::uint8_t>(i))]
                = decision_reason_counts[i];
        }
        json::object value{
            {"schema", "polymarket_v7_native_evidence_status_v1"},
            {"paper_only", true},
            {"authenticated_execution", false},
            {"real_order_submission", false},
            {"model_sha", config.model_sha},
            {"run_id", config.run_id},
            {"market_id", config.market_id},
            {"asset", config.asset},
            {"horizon", config.horizon},
            {"healthy", owner.healthy()},
            {"published", owner.published()},
            {"written", owner.written()},
            {"dropped", owner.dropped()},
            {"queue_depth", owner.queue_.approximate_size()},
            {"observation_capture_mode", config.observation_capture_mode},
            {"observations_published", owner.observations_published_.load()},
            {"observations_written", owner.observations_written_.load()},
            {"observations_dropped", owner.observations_dropped_.load()},
            {"observations_queue_depth", owner.observations_->approximate_size()},
            {"decision_observations", decision_observations},
            {"accepted_decision_observations", accepted_decision_observations},
            {"rejected_decision_observations",
                decision_observations - accepted_decision_observations},
            {"decision_reason_counts", std::move(reason_counts)},
            {"timestamp_ms", to_ms(wall_now_ns())},
        };
        const fs::path directory = fs::path(config.run_root) / "control" / "native_evidence";
        fs::create_directories(directory);
        atomic_write(directory / (config.market_id + ".json"),
                     json::serialize(value) + "\n");
    }

    void run() noexcept {
        try {
            fs::create_directories(fs::path(config.run_root) / "ledger" / "spool");
            fs::create_directories(fs::path(config.run_root) / "control");
            auto last_status = std::chrono::steady_clock::now();
            for (;;) {
                NativeEvidenceEvent event{};
                bool progressed = false;
                while (owner.queue_.try_pop(event)) {
                    progressed = true;
                    write_event(event);
                }
                NativeObservation observation{};
                for (std::size_t i = 0; i < 512 && owner.observations_->try_pop(observation); ++i) {
                    progressed = true;
                    write_observation(observation);
                }
                const auto now = std::chrono::steady_clock::now();
                if (now - last_status >= std::chrono::seconds(1)) {
                    if (observations_file.is_open()) observations_file.flush();
                    write_status();
                    last_status = now;
                }
                if (owner.stopping_.load(std::memory_order_acquire)
                    && owner.queue_.approximate_size() == 0
                    && owner.observations_->approximate_size() == 0) break;
                if (!progressed) std::this_thread::sleep_for(std::chrono::milliseconds(1));
            }
            if (observations_file.is_open()) {
                observations_file.flush();
                if (!observations_file) throw std::runtime_error("observation flush failed");
                observations_file.close();
                json::object closed{
                    {"schema", "polymarket_v7_native_capture_closed_v1"},
                    {"capture_id", capture_id}, {"run_id", config.run_id}, {"server_id", config.server_id},
                    {"market_id", config.market_id}, {"code_sha", config.model_sha},
                    {"closed", true}, {"healthy", owner.healthy()},
                    {"last_sequence", observation_sequence}, {"watermark_monotonic_ns", observations_watermark_ns},
                    {"bytes", fs::file_size(observations_path)},
                    {"source_delete_authorized", false},
                };
                atomic_write(observations_path.string() + ".closed.json", json::serialize(closed) + "\n");
            }
            write_status();
        } catch (...) {
            owner.healthy_.store(false, std::memory_order_release);
        }
    }
};

NativeRuntimeEvidenceWriter::NativeRuntimeEvidenceWriter(NativeRuntimeEvidenceConfig config) {
    observations_ = std::make_unique<SpscRing<NativeObservation, 8192>>();
    impl_ = std::make_unique<Impl>(std::move(config), *this);
}

NativeRuntimeEvidenceWriter::~NativeRuntimeEvidenceWriter() {
    stop();
}

bool NativeRuntimeEvidenceWriter::publish(const NativeEvidenceEvent& event) noexcept {
    if (!healthy() || stopping_.load(std::memory_order_acquire)) return false;
    if (!queue_.try_push(event)) {
        dropped_.fetch_add(1, std::memory_order_release);
        healthy_.store(false, std::memory_order_release);
        return false;
    }
    published_.fetch_add(1, std::memory_order_release);
    return true;
}

bool NativeRuntimeEvidenceWriter::publish_observation(const NativeObservation& event) noexcept {
    if (!healthy() || stopping_.load(std::memory_order_acquire)) return false;
    if (!observations_->try_push(event)) {
        observations_dropped_.fetch_add(1, std::memory_order_release);
        healthy_.store(false, std::memory_order_release);
        return false;
    }
    observations_published_.fetch_add(1, std::memory_order_release);
    return true;
}

void NativeRuntimeEvidenceWriter::stop() noexcept {
    if (!impl_) return;
    stopping_.store(true, std::memory_order_release);
    if (impl_->thread.joinable()) impl_->thread.join();
    impl_.reset();
}

} // namespace pm::v7
