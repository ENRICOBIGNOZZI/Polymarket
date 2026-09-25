#include "pm/v7_pure_arb_multi_ledger.hpp"

#include <boost/json.hpp>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <limits>
#include <sstream>
#include <thread>

namespace pm::v7 {
namespace fs = std::filesystem;
namespace json = boost::json;

namespace {
[[nodiscard]] std::int64_t wall_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}
[[nodiscard]] std::int64_t mono_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}
[[nodiscard]] std::int64_t ms(std::int64_t ns) noexcept {
    return ns > 0 ? ns / 1'000'000LL : 0;
}
[[nodiscard]] const char* side_name(Side side) noexcept {
    return side == Side::Buy ? "BUY" : side == Side::Sell ? "SELL" : "";
}
void atomic_write(const fs::path& path, const std::string& payload) {
    fs::create_directories(path.parent_path());
    const auto temp = fs::path(path.string() + ".tmp");
    {
        std::ofstream out(temp, std::ios::binary | std::ios::trunc);
        if (!out) throw std::runtime_error("multi ledger temp open failed");
        out.write(payload.data(), static_cast<std::streamsize>(payload.size()));
        out.flush();
        if (!out) throw std::runtime_error("multi ledger temp write failed");
    }
    fs::rename(temp, path);
}
} // namespace

struct PureArbMultiLedgerWriter::Impl {
    PureArbMultiLedgerConfig config;
    PureArbMultiLedgerWriter& owner;
    std::thread writer;
    std::int64_t wall_minus_mono = wall_ns() - mono_ns();
    std::uint64_t sequence = 0;

    Impl(PureArbMultiLedgerConfig value, PureArbMultiLedgerWriter& source)
        : config(std::move(value)), owner(source) {
        if (config.run_root.empty() || config.model_sha.size() != 40
            || config.run_id.empty() || config.server_id.empty()
            || config.risk_policy_sha256.size() != 64
            || config.markets.empty()) {
            throw std::invalid_argument("invalid multi ledger config");
        }
        writer = std::thread([this] { run(); });
    }

    [[nodiscard]] std::int64_t receive_ms(std::int64_t monotonic) const noexcept {
        if (monotonic <= 0 || wall_minus_mono <= 0
            || monotonic > std::numeric_limits<std::int64_t>::max() - wall_minus_mono) {
            return 0;
        }
        return ms(monotonic + wall_minus_mono);
    }

    [[nodiscard]] std::string token(
        const PureArbLedgerMarket& market, std::uint64_t instrument) const {
        if (instrument == market.yes_instrument_handle) return market.yes_token;
        if (instrument == market.no_instrument_handle) return market.no_token;
        throw std::runtime_error("multi ledger instrument mapping failed");
    }

    [[nodiscard]] double fee(
        const PureArbLedgerMarket& market,
        const NativePaperFillRecord& fill) const noexcept {
        if (fill.fill_microunits <= 0 || fill.tick_size_e4 <= 0 || fill.price_tick <= 0
            || fill.taker == 0 || market.fee_rate == 0.0) return 0.0;
        const double price = static_cast<double>(fill.price_tick)
            * static_cast<double>(fill.tick_size_e4) / 10'000.0;
        if (!(price > 0.0 && price < 1.0)) return 0.0;
        return static_cast<double>(fill.fill_microunits) / 1'000'000.0
            * market.fee_rate
            * std::pow(price * (1.0 - price), market.fee_exponent);
    }

    void write(const Envelope& envelope) {
        if (envelope.context_index >= config.markets.size())
            throw std::runtime_error("multi ledger context out of range");
        const auto& market = config.markets[envelope.context_index];
        const auto& fill = envelope.fill;
        const auto token_id = token(market, fill.instrument_handle);
        if (fill.fill_microunits <= 0 || fill.exchange_event_ns <= 0
            || fill.receive_monotonic_ns <= 0 || fill.tick_size_e4 <= 0
            || fill.price_tick <= 0 || fill.client_order_id == 0) {
            throw std::runtime_error("multi ledger invalid fill");
        }
        const auto receive = receive_ms(fill.receive_monotonic_ns);
        if (receive <= 0) throw std::runtime_error("multi ledger receive clock invalid");
        const auto recorded = ms(wall_ns());
        if (recorded < receive) throw std::runtime_error("multi ledger clock inversion");
        const double price = static_cast<double>(fill.price_tick)
            * static_cast<double>(fill.tick_size_e4) / 10'000.0;
        if (!(price > 0.0 && price <= 1.0))
            throw std::runtime_error("multi ledger price invalid");

        ++sequence;
        const std::string ordinal = std::to_string(sequence);
        const std::string order_id = "native:" + config.run_id + ":"
            + market.market_id + ":" + std::to_string(fill.client_order_id);
        const std::string record_id = config.run_id + ":" + market.market_id
            + ":native-multi:" + ordinal;
        const std::string fill_id = order_id + ":fill:" + ordinal;

        json::object receipt{
            {"schema","polymarket_v7_native_settlement_receipt_v1"},
            {"owner","V7_NATIVE_CRYPTO_SETTLEMENT_ENGINE"},
            {"engine_id","CRYPTO_SETTLEMENT_ENGINE"},
            {"model_sha",config.model_sha},
            {"asset",market.asset},{"horizon",market.horizon},
            {"paper_only",true},{"authenticated_execution",false},
            {"real_order_submission",false},{"real_capital_at_risk",false},
            {"execution_mode","PAPER_SIMULATED"},
            {"paper_simulation_authority",true},
            {"real_new_risk_authorized",false},
            {"single_owner",true},
            {"owner_chain",json::array{"portfolio","risk","capital","oms","inventory"}},
            {"order_namespace",config.run_id + ":" + market.market_id},
            {"client_order_id",fill.client_order_id},
            {"command_id",fill.command_id},
        };
        json::object metadata{
            {"component","pure_arb"},
            {"crypto_context",json::object{{"asset",market.asset},{"horizon",market.horizon}}},
            {"code_sha",config.model_sha},
            {"model_family","pure_arb"},
            {"paper_exploration",true},
            {"causal_arrival_verified",false},
            {"exchange_execution_verified",false},
            {"paper_simulator_semantics","PAIRED_FOK_ALL_OR_NONE_V1"},
            {"economic_authority","PAPER_EXPLORATION"},
            {"counterfactual",false},
            {"research_evidence_only",false},
            {"native_settlement_receipt",std::move(receipt)},
            {"run_id",config.run_id},{"server_id",config.server_id},
            {"asset",market.asset},{"horizon",market.horizon},
            {"risk_policy_sha256",config.risk_policy_sha256},
            {"context_index",envelope.context_index},
            {"paired_complete_set",true},
            {"local_receive_arrival_modelled",false},
            {"arrival_book_receive_monotonic_ns",fill.arrival_book_receive_ns},
            {"arrival_book_version",fill.arrival_book_version},
        };
        json::object value{
            {"schema_version",1},{"event_type","FILL"},
            {"strategy","CRYPTO_SETTLEMENT_ENGINE"},
            {"model_sha",config.model_sha},{"paper_only",true},
            {"authenticated_execution",false},
            {"record_id",record_id},{"recorded_ts_ms",recorded},
            {"model_version","native-pure-arb-multi-paper"},
            {"order_id",order_id},{"fill_id",fill_id},
            {"position_id","native-position:" + market.market_id + ":" + token_id},
            {"market_id",market.market_id},{"event_id",market.event_id},
            {"token_id",token_id},{"side",side_name(fill.side)},
            {"exchange_ts_ms",ms(fill.exchange_event_ns)},
            {"receive_ts_ms",receive},{"fill_price",price},
            {"filled_size",static_cast<double>(fill.fill_microunits)/1'000'000.0},
            {"complete",fill.order_state==OrderState::Filled},
            {"fee",fee(market,fill)},
            {"fee_rate",fill.taker!=0 ? market.fee_rate : 0.0},
            {"fee_source",market.fee_source},
            {"metadata",std::move(metadata)},
        };
        const fs::path directory = fs::path(config.run_root) / "ledger" / "spool";
        atomic_write(directory / (record_id + ".json"),
                     json::serialize(value) + "\n");
        owner.written_.fetch_add(1,std::memory_order_release);
    }

    void run() noexcept {
        try {
            fs::create_directories(fs::path(config.run_root) / "ledger" / "spool");
            for (;;) {
                Envelope envelope{};
                bool progressed=false;
                while (owner.queue_.try_pop(envelope)) {
                    progressed=true;
                    write(envelope);
                }
                if (owner.stopping_.load(std::memory_order_acquire)
                    && owner.queue_.approximate_size()==0) break;
                if (!progressed) std::this_thread::sleep_for(
                    std::chrono::milliseconds(1));
            }
        } catch (...) {
            owner.healthy_.store(false,std::memory_order_release);
        }
    }
};

PureArbMultiLedgerWriter::PureArbMultiLedgerWriter(
    PureArbMultiLedgerConfig config) {
    impl_=std::make_unique<Impl>(std::move(config),*this);
}
PureArbMultiLedgerWriter::~PureArbMultiLedgerWriter(){ stop(); }

bool PureArbMultiLedgerWriter::publish(
    std::size_t context_index,
    const NativePaperFillRecord& fill) noexcept {
    if (!healthy_.load(std::memory_order_acquire)
        || stopping_.load(std::memory_order_acquire)
        || context_index >= std::numeric_limits<std::uint16_t>::max()) return false;
    Envelope envelope{};
    envelope.context_index=static_cast<std::uint16_t>(context_index);
    envelope.fill=fill;
    if(!queue_.try_push(envelope)){
        dropped_.fetch_add(1,std::memory_order_release);
        healthy_.store(false,std::memory_order_release);
        return false;
    }
    published_.fetch_add(1,std::memory_order_release);
    return true;
}
void PureArbMultiLedgerWriter::stop() noexcept {
    if(!impl_) return;
    stopping_.store(true,std::memory_order_release);
    if(impl_->writer.joinable()) impl_->writer.join();
    impl_.reset();
}
PureArbMultiLedgerSnapshot PureArbMultiLedgerWriter::snapshot() const noexcept {
    return {published_.load(std::memory_order_acquire),
            written_.load(std::memory_order_acquire),
            dropped_.load(std::memory_order_acquire),
            queue_.approximate_size(),
            static_cast<std::uint8_t>(healthy_.load(std::memory_order_acquire))};
}

} // namespace pm::v7
