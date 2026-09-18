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
    std::uint64_t sequence = 0;

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
        const auto recorded_ms = to_ms(wall_now_ns());
        const auto order_id = std::string("native:") + std::to_string(event.command.client_order_id);
        const auto component = strategy_component(event.strategy_id);
        json::object metadata{
            {"component", component},
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
            {"record_id", config.run_id + ":" + config.market_id + ":native:" + std::to_string(sequence)},
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
        out["fill_id"] = "native:" + std::to_string(event.fill.client_order_id)
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

    void write_status() {
        json::object value{
            {"schema", "polymarket_v7_native_evidence_status_v1"},
            {"paper_only", true},
            {"authenticated_execution", false},
            {"real_order_submission", false},
            {"model_sha", config.model_sha},
            {"run_id", config.run_id},
            {"healthy", owner.healthy()},
            {"published", owner.published()},
            {"written", owner.written()},
            {"dropped", owner.dropped()},
            {"queue_depth", owner.queue_.approximate_size()},
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
                const auto now = std::chrono::steady_clock::now();
                if (now - last_status >= std::chrono::seconds(1)) {
                    write_status();
                    last_status = now;
                }
                if (owner.stopping_.load(std::memory_order_acquire)
                    && owner.queue_.approximate_size() == 0) break;
                if (!progressed) std::this_thread::sleep_for(std::chrono::milliseconds(1));
            }
            write_status();
        } catch (...) {
            owner.healthy_.store(false, std::memory_order_release);
        }
    }
};

NativeRuntimeEvidenceWriter::NativeRuntimeEvidenceWriter(NativeRuntimeEvidenceConfig config) {
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

void NativeRuntimeEvidenceWriter::stop() noexcept {
    if (!impl_) return;
    stopping_.store(true, std::memory_order_release);
    if (impl_->thread.joinable()) impl_->thread.join();
    impl_.reset();
}

} // namespace pm::v7
