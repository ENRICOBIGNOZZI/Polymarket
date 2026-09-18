#include "pm/v7_native_runtime_evidence.hpp"

#include <boost/json.hpp>

#include <chrono>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <thread>

namespace pm::v7 {
namespace fs = std::filesystem;
namespace json = boost::json;
namespace {

[[nodiscard]] bool exact_sha(const std::string& value) noexcept {
    if (value.size() != 40) return false;
    for (const char ch : value) {
        if (!((ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f'))) return false;
    }
    return true;
}

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
        && !server_id.empty() && !market_id.empty() && !event_id.empty()
        && !yes_token_id.empty() && !no_token_id.empty()
        && !fee_source.empty() && yes_instrument_handle != 0
        && no_instrument_handle != 0 && yes_instrument_handle != no_instrument_handle
        && close_wall_ns > 0 && std::isfinite(taker_fee_rate) && taker_fee_rate >= 0.0
        && std::isfinite(taker_fee_exponent) && taker_fee_exponent >= 0.0;
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

    [[nodiscard]] std::int64_t wall_ms_from_monotonic(std::int64_t ns) const noexcept {
        if (ns <= 0 || wall_minus_monotonic_ns <= 0
            || ns > std::numeric_limits<std::int64_t>::max() - wall_minus_monotonic_ns) return 0;
        return to_ms(ns + wall_minus_monotonic_ns);
    }

    [[nodiscard]] json::object receipt(const NativeOrderCommand& command) const {
        return {
            {"schema", "polymarket_v7_native_settlement_receipt_v1"},
            {"owner", "V7_NATIVE_CRYPTO_SETTLEMENT_ENGINE"},
            {"engine_id", "CRYPTO_SETTLEMENT_ENGINE"},
            {"model_sha", config.model_sha},
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
        json::object metadata{
            {"component", component},
            {"crypto_context", json::object{{"asset", config.asset}, {"horizon", config.horizon}}},
            {"code_sha", config.model_sha},
            {"model_artifact_hash", event.strategy_id == StrategyId::ProfessionalMaker && !config.maker_artifact_sha256.empty()
                ? json::value(config.maker_artifact_sha256) : json::value(nullptr)},
            {"maker_policy_sha256", config.maker_policy_sha256.empty() ? json::value(nullptr) : json::value(config.maker_policy_sha256)},
            {"prediction_model_kind", event.strategy_id == StrategyId::ProfessionalMaker
                ? (config.maker_valid_cells > 0 ? "EXECUTION_CELLS_LOADED" : "DEFAULT_BASELINE")
                : "FROZEN_DIRECTIONAL_RULE"},
            {"maker_valid_cells", config.maker_valid_cells},
            {"model_family", component},
            {"paper_exploration", true},
            {"economic_authority", "PAPER_EXPLORATION"},
            {"counterfactual", false},
            {"research_evidence_only", false},
            {"native_settlement_receipt", receipt(event.command)},
            {"run_id", config.run_id},
            {"server_id", config.server_id},
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
            return out;
        }
        if (event.kind == NativeEvidenceKind::OrderState) {
            auto out = base(event, "ORDER_STATE");
            out["order_state"] = state_name(event.order_state);
            return out;
        }
        auto out = base(event, "FILL");
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
        if (!observations_file.is_open()) {
            const auto directory = fs::path(config.run_root) / "research/native_observations" / config.run_id;
            fs::create_directories(directory);
            observations_path = directory / (config.market_id + "-" + capture_id + ".jsonl");
            observations_file.open(observations_path, std::ios::app);
            if (!observations_file) throw std::runtime_error("native observations open failed");
        }
        json::array bids, asks;
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
            {"run_id", config.run_id}, {"server_id", config.server_id},
            {"market_id", config.market_id}, {"token_id", token(event.instrument_handle)},
            {"asset", config.asset}, {"horizon", config.horizon},
            {"capture_id", capture_id}, {"connection_epoch", event.connection_epoch},
            {"sequence", ++observation_sequence}, {"kind", event.kind},
            {"event_receive_monotonic_ns", event.event_receive_ns}, {"event_exchange_ns", event.event_exchange_ns},
            {"native_event_kind", event.event_kind},
            {"minimum_order_microunits", config.minimum_order_microunits},
            {"risk_policy_sha256", config.risk_policy_sha256}, {"fee_source", config.fee_source},
            {"receive_monotonic_ns", event.receive_ns}, {"exchange_event_ns", event.exchange_ns},
            {"observed_monotonic_ns", event.observed_ns}, {"trigger_monotonic_ns", event.trigger_ns},
            {"decision_monotonic_ns", event.decision_ns}, {"close_monotonic_ns", event.close_ns},
            {"receive_wall_ms", wall_ms_from_monotonic(event.receive_ns)},
            {"signal_version", event.signal_version}, {"book_version", event.book_version},
            {"signal_return_bp", std::isfinite(event.signal_return_bp) ? json::value(event.signal_return_bp) : json::value(nullptr)}, {"direction", event.direction},
            {"reason", event.reason}, {"accepted", event.accepted != 0}, {"book_valid", event.valid != 0},
            {"bid_e4", event.bid_e4}, {"ask_e4", event.ask_e4}, {"tick_e4", event.tick_e4},
            {"bid_quantity", event.bid_quantity}, {"ask_quantity", event.ask_quantity},
            {"bids", std::move(bids)}, {"asks", std::move(asks)},
            {"trade_side", event.trade_side}, {"trade_e4", event.trade_e4}, {"trade_quantity", event.trade_quantity},
            {"fee_rate", config.taker_fee_rate}, {"fee_exponent", config.taker_fee_exponent},
            {"proposed_quantity", event.proposed_quantity}, {"proposed_price_tick", event.proposed_price_tick},
            {"ev_uncertainty", event.kind == 4 ? json::value(event.ev_uncertainty) : json::value(nullptr)},
            {"probability_forecast", nullptr},
            {"expected_net_edge", event.kind == 4 ? json::value(event.expected_ev) : json::value(nullptr)},
        };
        observations_file << json::serialize(value) << '\n';
        if (!observations_file) throw std::runtime_error("native observations write failed");
        observations_watermark_ns = std::max(observations_watermark_ns, event.observed_ns);
        owner.observations_written_.fetch_add(1, std::memory_order_release);
    }

    void write_status() {
        json::object value{
            {"schema", "polymarket_v7_native_evidence_status_v1"},
            {"paper_only", true},
            {"authenticated_execution", false},
            {"real_order_submission", false},
            {"model_sha", config.model_sha},
            {"run_id", config.run_id},
            {"healthy", owner.healthy()},
            {"market_id", config.market_id},
            {"published", owner.published()},
            {"written", owner.written()},
            {"dropped", owner.dropped()},
            {"queue_depth", owner.queue_.approximate_size()},
            {"observations_published", owner.observations_published_.load()},
            {"observations_written", owner.observations_written_.load()},
            {"observations_dropped", owner.observations_dropped_.load()},
            {"observations_queue_depth", owner.observations_->approximate_size()},
            {"timestamp_ms", to_ms(wall_now_ns())},
        };
        atomic_write(fs::path(config.run_root) / "control" / "native_evidence_status.json",
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
