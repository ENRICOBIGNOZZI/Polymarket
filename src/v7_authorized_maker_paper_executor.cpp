#include "pm/v7_maker_paper.hpp"

#include <boost/json.hpp>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <memory>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <unordered_map>
#include <utility>
#include <vector>

namespace {
namespace fs = std::filesystem;
namespace json = boost::json;
using pm::v7::IntentPurpose;
using pm::v7::IntentType;
using pm::v7::PublicTradePrint;
using pm::v7::Side;
using pm::v7::StrategyId;
using pm::v7::StrategyIntent;
using pm::v7::Urgency;
using pm::v7::maker::MakerPaperMarketEngine;
using pm::v7::maker::PaperExecutionOutcome;
using pm::v7::maker::PaperMakerEvent;
using pm::v7::maker::PaperMakerEventKind;
using pm::v7::maker::PaperMakerPolicy;
using pm::v7::maker::PaperMakerResult;

constexpr double kMicrounitsPerShare = 1'000'000.0;
constexpr double kPriceScaleE4 = 10'000.0;
constexpr std::int64_t kSelectionMaxAgeMs = 5'000;
constexpr std::int64_t kFillabilityStatusMaxAgeMs = 5'000;

[[nodiscard]] std::int64_t wall_ms() noexcept {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

[[nodiscard]] std::int64_t wall_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

[[nodiscard]] std::int64_t monotonic_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

[[nodiscard]] bool exact_sha(std::string_view value) noexcept {
    if (value.size() != 40) return false;
    for (const char ch : value) {
        if (!((ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f'))) return false;
    }
    return true;
}

[[nodiscard]] std::uint64_t fnv1a(std::string_view value) noexcept {
    std::uint64_t hash = 1469598103934665603ULL;
    for (const unsigned char ch : value) {
        hash ^= static_cast<std::uint64_t>(ch);
        hash *= 1099511628211ULL;
    }
    return hash == 0 ? 1 : hash;
}

[[nodiscard]] std::uint64_t mix64(std::uint64_t x) noexcept {
    x += 0x9e3779b97f4a7c15ULL;
    x = (x ^ (x >> 30U)) * 0xbf58476d1ce4e5b9ULL;
    x = (x ^ (x >> 27U)) * 0x94d049bb133111ebULL;
    return x ^ (x >> 31U);
}

[[nodiscard]] std::string hex64(std::uint64_t value) {
    std::ostringstream out;
    out << std::hex << std::setw(16) << std::setfill('0') << value;
    return out.str();
}

[[nodiscard]] std::string read_file(const fs::path& path) {
    std::ifstream input(path);
    if (!input) throw std::runtime_error("cannot open " + path.string());
    std::ostringstream buffer;
    buffer << input.rdbuf();
    return buffer.str();
}

[[nodiscard]] json::value read_json(const fs::path& path) {
    boost::system::error_code error;
    auto value = json::parse(read_file(path), error);
    if (error) throw std::runtime_error("invalid json " + path.string() + ": " + error.message());
    return value;
}

void atomic_write(const fs::path& path, std::string_view content) {
    fs::create_directories(path.parent_path());
    const fs::path temporary = path.string() + ".tmp." + std::to_string(::getpid());
    {
        std::ofstream output(temporary, std::ios::trunc);
        if (!output) throw std::runtime_error("cannot write " + temporary.string());
        output.write(content.data(), static_cast<std::streamsize>(content.size()));
        output.flush();
        if (!output) throw std::runtime_error("failed writing " + temporary.string());
    }
    std::error_code error;
    fs::rename(temporary, path, error);
    if (error) {
        fs::remove(path, error);
        error.clear();
        fs::rename(temporary, path, error);
        if (error) throw std::runtime_error("cannot replace " + path.string());
    }
}

[[nodiscard]] const json::value* find_value(const json::object& object, std::string_view key) noexcept {
    const auto it = object.find(key);
    return it == object.end() ? nullptr : &it->value();
}

[[nodiscard]] const json::object* child_object(const json::object& object, std::string_view key) noexcept {
    const auto* value = find_value(object, key);
    return value != nullptr && value->is_object() ? &value->as_object() : nullptr;
}

[[nodiscard]] const json::array* child_array(const json::object& object, std::string_view key) noexcept {
    const auto* value = find_value(object, key);
    return value != nullptr && value->is_array() ? &value->as_array() : nullptr;
}

[[nodiscard]] std::string text(const json::value* value) {
    if (value == nullptr) return {};
    if (value->is_string()) return std::string(value->as_string());
    if (value->is_int64()) return std::to_string(value->as_int64());
    if (value->is_uint64()) return std::to_string(value->as_uint64());
    return {};
}

[[nodiscard]] bool boolean(const json::value* value, bool fallback = false) noexcept {
    return value != nullptr && value->is_bool() ? value->as_bool() : fallback;
}

[[nodiscard]] double number(const json::value* value, double fallback = 0.0) noexcept {
    if (value == nullptr) return fallback;
    if (value->is_double()) return value->as_double();
    if (value->is_int64()) return static_cast<double>(value->as_int64());
    if (value->is_uint64()) return static_cast<double>(value->as_uint64());
    return fallback;
}

[[nodiscard]] std::int64_t integer(const json::value* value, std::int64_t fallback = 0) noexcept {
    if (value == nullptr) return fallback;
    if (value->is_int64()) return value->as_int64();
    if (value->is_uint64()) {
        const auto raw = value->as_uint64();
        return raw <= static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max())
            ? static_cast<std::int64_t>(raw) : fallback;
    }
    if (value->is_double() && std::isfinite(value->as_double())) {
        return static_cast<std::int64_t>(value->as_double());
    }
    return fallback;
}

[[nodiscard]] std::int64_t shares_to_micro(double shares) noexcept {
    if (!std::isfinite(shares) || shares <= 0.0) return 0;
    const long double raw = static_cast<long double>(shares) * kMicrounitsPerShare;
    if (raw > static_cast<long double>(std::numeric_limits<std::int64_t>::max())) return 0;
    return static_cast<std::int64_t>(std::llround(raw));
}

[[nodiscard]] double micro_to_shares(std::int64_t value) noexcept {
    return static_cast<double>(std::max<std::int64_t>(0, value)) / kMicrounitsPerShare;
}

struct Options {
    fs::path run_root = "runs/paper_v7_live";
    std::string model_sha;
    bool once = false;
};

[[nodiscard]] Options parse_options(int argc, char** argv) {
    Options options;
    for (int index = 1; index < argc; ++index) {
        const std::string arg = argv[index];
        auto next = [&]() -> std::string {
            if (++index >= argc) throw std::runtime_error("missing value after " + arg);
            return argv[index];
        };
        if (arg == "--run-root") options.run_root = next();
        else if (arg == "--model-sha") options.model_sha = next();
        else if (arg == "--once") options.once = true;
        else throw std::runtime_error("unknown argument: " + arg);
    }
    if (!exact_sha(options.model_sha)) throw std::runtime_error("--model-sha must be exact 40-hex SHA");
    return options;
}

struct SelectionEvidence {
    std::int64_t generated_at_ms = 0;
    std::int32_t tick_size_e4 = 0;
    double best_bid = 0.0;
    double best_ask = 0.0;
    double queue_ahead_shares = 0.0;
    bool found = false;
};

[[nodiscard]] SelectionEvidence selection_evidence(
    const fs::path& path, std::string_view model_sha, std::string_view market_id,
    std::string_view token_id) {
    SelectionEvidence result;
    const auto value = read_json(path);
    if (!value.is_object()) return result;
    const auto& root = value.as_object();
    if (!boolean(find_value(root, "paper_only"))
        || boolean(find_value(root, "authenticated_execution"), true)
        || boolean(find_value(root, "real_order_submission"), true)
        || text(find_value(root, "model_sha")) != model_sha) {
        return result;
    }
    result.generated_at_ms = integer(find_value(root, "generated_at_ms"));
    const auto* markets = child_array(root, "markets");
    if (markets == nullptr) return result;
    for (const auto& item : *markets) {
        if (!item.is_object()) continue;
        const auto& market = item.as_object();
        if (text(find_value(market, "market_id")) != market_id) continue;
        const auto* opportunities = child_array(market, "quote_opportunities");
        if (opportunities == nullptr) continue;
        for (const auto& raw : *opportunities) {
            if (!raw.is_object()) continue;
            const auto& quote = raw.as_object();
            if (text(find_value(quote, "token_id")) != token_id) continue;
            const double tick = number(find_value(quote, "tick_size"));
            if (!(tick > 0.0 && tick < 1.0)) continue;
            const auto tick_e4 = static_cast<std::int32_t>(std::llround(tick * kPriceScaleE4));
            if (tick_e4 <= 0 || 10'000 % tick_e4 != 0) continue;
            result.tick_size_e4 = tick_e4;
            result.best_bid = number(find_value(quote, "best_bid"));
            result.best_ask = number(find_value(quote, "best_ask"));
            result.queue_ahead_shares = std::max(0.0, number(find_value(quote, "queue_ahead_shares")));
            result.found = result.best_bid > 0.0 && result.best_ask > result.best_bid && result.best_ask < 1.0;
            return result;
        }
    }
    return result;
}

struct FillabilityStatus {
    std::int64_t last_exchange_event_ns = 0;
    bool valid = false;
};

[[nodiscard]] FillabilityStatus fillability_status(
    const fs::path& path, std::string_view model_sha) {
    FillabilityStatus result;
    const auto value = read_json(path);
    if (!value.is_object()) return result;
    const auto& root = value.as_object();
    const auto now = wall_ms();
    const auto timestamp_ms = integer(find_value(root, "timestamp_ms"));
    result.last_exchange_event_ns = integer(find_value(root, "last_exchange_event_ns"));
    result.valid = text(find_value(root, "schema")) == "polymarket_v7_maker_fillability_ws_status_v1"
        && text(find_value(root, "model_sha")) == model_sha
        && text(find_value(root, "state")) == "running"
        && boolean(find_value(root, "paper_only"))
        && !boolean(find_value(root, "authenticated_execution"), true)
        && !boolean(find_value(root, "real_order_submission"), true)
        && boolean(find_value(root, "evidence_complete"))
        && timestamp_ms > 0 && now >= timestamp_ms && now - timestamp_ms <= kFillabilityStatusMaxAgeMs
        && result.last_exchange_event_ns > 0;
    return result;
}

struct Authorization {
    fs::path source_path;
    json::object receipt;
    json::object envelope;
    std::string replay_key;
    std::string market_id;
    std::string event_id;
    std::string token_id;
    std::string outcome;
    Side order_side = Side::None;
    double limit_price = 0.0;
    double quantity_shares = 0.0;
    double conservative_ev = 0.0;
    double fill_probability = 0.0;
    double queue_ahead_shares = 0.0;
    std::uint32_t horizon_ms = 0;
    std::int64_t expires_at_ns = 0;
};

[[nodiscard]] Authorization parse_authorization(
    const fs::path& path, std::string_view model_sha) {
    const auto value = read_json(path);
    if (!value.is_object()) throw std::runtime_error("authorization not object");
    const auto& root = value.as_object();
    if (text(find_value(root, "schema")) != "polymarket_v7_authorized_make_intent_v1"
        || !boolean(find_value(root, "paper_only"))
        || boolean(find_value(root, "authenticated_execution"), true)
        || boolean(find_value(root, "real_order_submission"), true)
        || boolean(find_value(root, "real_capital_at_risk"), true)
        || text(find_value(root, "owner")) != "V7_GLOBAL_PORTFOLIO_COORDINATOR"
        || text(find_value(root, "execution_authority")) != "SIMULATED_PAPER_ONLY") {
        throw std::runtime_error("authorization safety/owner invalid");
    }
    const auto* decision = child_object(root, "decision");
    const auto* envelope = child_object(root, "opportunity_envelope");
    if (decision == nullptr || envelope == nullptr) throw std::runtime_error("authorization payload missing");
    const auto* crypto = child_object(*envelope, "crypto_context");
    if (text(find_value(*decision, "schema")) != "polymarket_v7_global_opportunity_decision_v1"
        || text(find_value(*decision, "owner")) != "V7_GLOBAL_PORTFOLIO_COORDINATOR"
        || text(find_value(*decision, "action")) != "MAKE"
        || text(find_value(*decision, "engine_id")) != "CRYPTO_SETTLEMENT_ENGINE"
        || boolean(find_value(*decision, "new_risk_authorized"), true)
        || !boolean(find_value(*decision, "paper_exploration_authorized"))
        || !boolean(find_value(*decision, "paper_only"))
        || boolean(find_value(*decision, "authenticated_execution"), true)
        || boolean(find_value(*decision, "real_order_submission"), true)
        || boolean(find_value(*decision, "real_capital_at_risk"), true)
        || text(find_value(*envelope, "schema")) != "polymarket_v7_opportunity_envelope_v1"
        || text(find_value(*envelope, "model_sha")) != model_sha
        || text(find_value(*envelope, "engine_id")) != "CRYPTO_SETTLEMENT_ENGINE"
        || text(find_value(*envelope, "action")) != "MAKE"
        || crypto == nullptr
        || text(find_value(*crypto, "asset")) != "BTC"
        || text(find_value(*crypto, "horizon")) != "M5"
        || text(find_value(*crypto, "authority")) != "PAPER_EXPLORATION"
        || boolean(find_value(*crypto, "research_only"), true)) {
        throw std::runtime_error("authorization decision/envelope invalid");
    }
    const std::string replay_key = text(find_value(*envelope, "deterministic_replay_key"));
    if (replay_key.empty() || text(find_value(*decision, "selected_replay_key")) != replay_key) {
        throw std::runtime_error("authorization replay identity mismatch");
    }
    const auto* plan = child_object(*envelope, "execution_plan");
    const auto* legs = plan == nullptr ? nullptr : child_array(*plan, "legs");
    if (legs == nullptr || legs->size() != 1 || !(*legs)[0].is_object()) {
        throw std::runtime_error("authorization requires single leg");
    }
    const auto& leg = (*legs)[0].as_object();
    const std::string side = text(find_value(leg, "side"));
    // The first unified PAPER maker lane is deliberately BUY-only.  SELL needs
    // coordinator-owned inventory availability and split/merge state before it
    // can be admitted without creating a second inventory authority.
    if (side != "BUY") throw std::runtime_error("authorized maker executor is BUY-only until inventory bridge exists");

    Authorization out;
    out.source_path = path;
    out.receipt = *decision;
    out.envelope = *envelope;
    out.replay_key = replay_key;
    out.market_id = text(find_value(*envelope, "market_id"));
    out.event_id = text(find_value(*envelope, "event_id"));
    out.token_id = text(find_value(leg, "token_id"));
    out.outcome = text(find_value(*envelope, "side"));
    out.order_side = Side::Buy;
    out.limit_price = number(find_value(leg, "limit_price"));
    out.quantity_shares = number(find_value(leg, "target_quantity"));
    out.conservative_ev = number(find_value(*envelope, "conservative_expected_wealth_change"));
    out.horizon_ms = static_cast<std::uint32_t>(std::max<std::int64_t>(0, integer(find_value(*plan, "timeout_ms"))));
    out.expires_at_ns = integer(find_value(*envelope, "expires_at_ns"));
    const auto* alpha = child_object(*envelope, "execution_alpha");
    const auto* fill = alpha == nullptr ? nullptr : child_object(*alpha, "fill_probability");
    const auto* features = alpha == nullptr ? nullptr : child_object(*alpha, "features");
    out.fill_probability = fill == nullptr ? 0.0 : number(find_value(*fill, "point"));
    out.queue_ahead_shares = features == nullptr ? 0.0 : std::max(0.0, number(find_value(*features, "queue_ahead")));
    if (out.market_id.empty() || out.event_id.empty() || out.token_id.empty()
        || (out.outcome != "YES" && out.outcome != "NO")
        || !(out.limit_price > 0.0 && out.limit_price < 1.0)
        || !(out.quantity_shares > 0.0)
        || !(out.conservative_ev > 0.0)
        || !(out.fill_probability > 0.0 && out.fill_probability <= 1.0)
        || out.expires_at_ns <= wall_ns()) {
        throw std::runtime_error("authorization economics/ttl invalid");
    }
    return out;
}

struct OrderContext {
    Authorization authorization;
    std::uint64_t order_id = 0;
    std::uint64_t instrument_handle = 0;
    std::int32_t tick_size_e4 = 0;
    fs::path live_authorization_path;
    bool terminal = false;
};

struct MarketRuntime {
    std::uint64_t market_handle = 0;
    std::uint64_t yes_handle = 0;
    std::uint64_t no_handle = 0;
    std::unique_ptr<MakerPaperMarketEngine> engine;
};

class Executor final {
public:
    explicit Executor(Options options)
        : options_(std::move(options)),
          authorization_dir_(options_.run_root / "micro_maker" / "authorized_make"),
          live_dir_(authorization_dir_ / "live"),
          archive_dir_(authorization_dir_ / "archive"),
          orphaned_dir_(authorization_dir_ / "orphaned"),
          rejected_dir_(authorization_dir_ / "rejected"),
          selection_path_(options_.run_root / "micro_maker" / "reward_selection.json"),
          fillability_status_path_(options_.run_root / "micro_maker" / "fillability_ws_status.json"),
          trade_tape_path_(options_.run_root / "micro_maker" / "fillability_ws.jsonl"),
          status_path_(options_.run_root / "micro_maker" / "authorized_make_executor_status.json"),
          spool_dir_(options_.run_root / "ledger" / "spool") {
        fs::create_directories(authorization_dir_);
        fs::create_directories(live_dir_);
        fs::create_directories(archive_dir_);
        fs::create_directories(orphaned_dir_);
        fs::create_directories(rejected_dir_);
        fs::create_directories(spool_dir_);
        quarantine_restart_live_authorizations();
        open_trade_tape_at_end();
    }

    void run_once() {
        process_authorizations();
        drain_trade_tape();
        advance_time();
        write_status();
    }

    [[nodiscard]] std::size_t active_orders() const noexcept { return orders_.size(); }

private:
    void quarantine_restart_live_authorizations() {
        std::error_code error;
        for (const auto& entry : fs::directory_iterator(live_dir_, error)) {
            if (error || !entry.is_regular_file() || entry.path().extension() != ".json") continue;
            fs::rename(entry.path(), orphaned_dir_ / entry.path().filename(), error);
            if (!error) ++restart_orphans_;
            error.clear();
        }
    }

    void open_trade_tape_at_end() {
        trade_tape_.open(trade_tape_path_);
        if (!trade_tape_) return;
        trade_tape_.seekg(0, std::ios::end);
        trade_offset_ = trade_tape_.tellg();
    }

    [[nodiscard]] MarketRuntime& market_runtime(std::string_view market_id) {
        const std::string key(market_id);
        auto it = markets_.find(key);
        if (it != markets_.end()) return it->second;
        MarketRuntime runtime;
        runtime.market_handle = fnv1a(key);
        runtime.yes_handle = fnv1a(key + ":YES");
        runtime.no_handle = fnv1a(key + ":NO");
        if (runtime.yes_handle == runtime.no_handle) runtime.no_handle = mix64(runtime.no_handle);
        PaperMakerPolicy policy;
        runtime.engine = std::make_unique<MakerPaperMarketEngine>(
            runtime.market_handle, runtime.yes_handle, runtime.no_handle, policy);
        auto [inserted, _] = markets_.emplace(key, std::move(runtime));
        return inserted->second;
    }

    void process_authorizations() {
        std::error_code error;
        std::vector<fs::path> paths;
        for (const auto& entry : fs::directory_iterator(authorization_dir_, error)) {
            if (error || !entry.is_regular_file() || entry.path().extension() != ".json") continue;
            paths.push_back(entry.path());
        }
        std::sort(paths.begin(), paths.end());
        for (const auto& path : paths) {
            try {
                submit(path);
            } catch (const std::exception& exc) {
                ++rejected_authorizations_;
                last_error_ = exc.what();
                move_file(path, rejected_dir_);
            }
        }
    }

    void submit(const fs::path& path) {
        Authorization authorization = parse_authorization(path, options_.model_sha);
        const SelectionEvidence selection = selection_evidence(
            selection_path_, options_.model_sha, authorization.market_id, authorization.token_id);
        const auto now = wall_ms();
        if (!selection.found || selection.generated_at_ms <= 0 || now < selection.generated_at_ms
            || now - selection.generated_at_ms > kSelectionMaxAgeMs) {
            throw std::runtime_error("fresh maker selection unavailable");
        }
        if (authorization.limit_price >= selection.best_ask - 1e-12) {
            throw std::runtime_error("post-only arrival revalidation failed");
        }
        const FillabilityStatus fillability = fillability_status(fillability_status_path_, options_.model_sha);
        if (!fillability.valid) throw std::runtime_error("fillability tape/status not causally ready");

        MarketRuntime& market = market_runtime(authorization.market_id);
        const std::uint64_t instrument = authorization.outcome == "YES" ? market.yes_handle : market.no_handle;
        const double tick = static_cast<double>(selection.tick_size_e4) / kPriceScaleE4;
        const auto price_tick = static_cast<std::int64_t>(std::llround(authorization.limit_price / tick));
        if (price_tick <= 0 || std::abs(price_tick * tick - authorization.limit_price) > 1e-8) {
            throw std::runtime_error("authorized price is off tick");
        }
        const auto quantity = shares_to_micro(authorization.quantity_shares);
        if (quantity <= 0) throw std::runtime_error("authorized quantity invalid");
        StrategyIntent intent;
        intent.intent_id = mix64(fnv1a(authorization.replay_key));
        intent.market_handle = market.market_handle;
        intent.event_handle = fnv1a(authorization.event_id);
        intent.instrument_handle = instrument;
        intent.decision_monotonic_ns = monotonic_ns();
        intent.exchange_event_ns = fillability.last_exchange_event_ns;
        intent.price_tick = price_tick;
        intent.quantity_microunits = quantity;
        intent.horizon_ms = std::max<std::uint32_t>(1, authorization.horizon_ms);
        intent.strategy_id = StrategyId::ProfessionalMaker;
        intent.type = IntentType::Quote;
        intent.side = Side::Buy;
        intent.urgency = Urgency::Passive;
        intent.purpose = IntentPurpose::Alpha;
        intent.passive = 1;
        intent.post_only = 1;
        intent.expected_edge = authorization.conservative_ev / authorization.quantity_shares;
        intent.expected_cost = 0.0;
        intent.expected_risk = 0.0;
        intent.expected_ev = authorization.conservative_ev;
        intent.ev_uncertainty = 0.0;

        const auto visible_queue = shares_to_micro(std::max(
            authorization.queue_ahead_shares, selection.queue_ahead_shares));
        PaperMakerResult result = market.engine->apply_intent(
            intent, visible_queue, selection.tick_size_e4);
        if (!result.applied || result.rejected || result.invariant_violation) {
            throw std::runtime_error("queue-aware paper engine rejected authorized MAKE");
        }
        const PaperMakerEvent* live = nullptr;
        for (std::size_t index = 0; index < result.event_count; ++index) {
            if (result.events[index].kind == PaperMakerEventKind::OrderLive) {
                live = &result.events[index];
                break;
            }
        }
        if (live == nullptr || live->order_id == 0) {
            throw std::runtime_error("paper engine did not emit OrderLive");
        }
        const fs::path live_path = live_dir_ / path.filename();
        move_file(path, live_dir_);
        OrderContext context;
        context.authorization = std::move(authorization);
        context.order_id = live->order_id;
        context.instrument_handle = instrument;
        context.tick_size_e4 = selection.tick_size_e4;
        context.live_authorization_path = live_path;
        const std::uint64_t order_id = context.order_id;
        token_to_order_[context.authorization.token_id] = order_id;
        orders_.emplace(order_id, std::move(context));
        emit_order_submitted(*live, orders_.at(order_id));
        ++submitted_orders_;
    }

    void drain_trade_tape() {
        if (!trade_tape_.is_open()) {
            open_trade_tape_at_end();
            return;
        }
        trade_tape_.clear();
        if (trade_offset_ >= 0) trade_tape_.seekg(trade_offset_);
        std::string line;
        while (std::getline(trade_tape_, line)) {
            trade_offset_ = trade_tape_.tellg();
            if (trade_offset_ < 0) trade_offset_ = static_cast<std::streamoff>(trade_tape_.rdbuf()->pubseekoff(0, std::ios::cur, std::ios::in));
            if (line.empty()) continue;
            process_trade_line(line);
        }
        trade_tape_.clear();
    }

    void process_trade_line(std::string_view line) {
        boost::system::error_code error;
        auto value = json::parse(line, error);
        if (error || !value.is_object()) { ++invalid_trade_rows_; return; }
        const auto& row = value.as_object();
        if (text(find_value(row, "schema")) != "polymarket_v7_maker_fillability_ws_trade_v1"
            || text(find_value(row, "model_sha")) != options_.model_sha
            || !boolean(find_value(row, "paper_only"))
            || boolean(find_value(row, "authenticated_execution"), true)
            || boolean(find_value(row, "real_order_submission"), true)
            || !boolean(find_value(row, "lineage_continuous"))) {
            ++invalid_trade_rows_;
            return;
        }
        const std::string token = text(find_value(row, "token_id"));
        const auto token_it = token_to_order_.find(token);
        if (token_it == token_to_order_.end()) return;
        const auto order_it = orders_.find(token_it->second);
        if (order_it == orders_.end() || order_it->second.terminal) return;
        OrderContext& context = order_it->second;
        auto market_it = markets_.find(context.authorization.market_id);
        if (market_it == markets_.end()) return;
        const double trade_price = number(find_value(row, "price"));
        const double trade_size = number(find_value(row, "size"));
        const auto exchange_ns = integer(find_value(row, "exchange_event_ns"));
        const auto receive_monotonic = integer(find_value(row, "receive_monotonic_ns"));
        const auto receive_wall = integer(find_value(row, "receive_wall_ms"));
        const auto observer_sequence = static_cast<std::uint64_t>(
            std::max<std::int64_t>(0, integer(find_value(row, "observer_sequence"))));
        const std::string aggressor = text(find_value(row, "aggressor_side"));
        if (!(trade_price > 0.0 && trade_price < 1.0) || !(trade_size > 0.0)
            || exchange_ns <= 0 || receive_monotonic <= 0 || receive_wall <= 0
            || observer_sequence == 0 || (aggressor != "BUY" && aggressor != "SELL")) {
            ++invalid_trade_rows_;
            return;
        }
        const double tick = static_cast<double>(context.tick_size_e4) / kPriceScaleE4;
        const auto price_tick = static_cast<std::int64_t>(std::llround(trade_price / tick));
        const auto quantity = shares_to_micro(trade_size);
        if (price_tick <= 0 || quantity <= 0) { ++invalid_trade_rows_; return; }
        PublicTradePrint trade;
        trade.trade_id = mix64(observer_sequence ^ fnv1a(token));
        trade.instrument_handle = context.instrument_handle;
        trade.aggressor_side = aggressor == "BUY" ? Side::Buy : Side::Sell;
        trade.price_tick = price_tick;
        trade.quantity_microunits = quantity;
        trade.exchange_event_ns = exchange_ns;
        trade.receive_monotonic_ns = receive_monotonic;
        PaperMakerResult result = market_it->second.engine->on_public_trade(trade);
        ++trade_rows_consumed_;
        handle_result(result, context.authorization.market_id, &row);
    }

    void advance_time() {
        const auto now = monotonic_ns();
        for (auto& [market_id, market] : markets_) {
            PaperMakerResult result = market.engine->advance_time(now);
            handle_result(result, market_id, nullptr);
        }
    }

    void handle_result(
        const PaperMakerResult& result, std::string_view market_id,
        const json::object* trade_row) {
        for (std::size_t index = 0; index < result.event_count; ++index) {
            const auto& event = result.events[index];
            if (event.order_id == 0) continue;
            auto it = orders_.find(event.order_id);
            if (it == orders_.end()) continue;
            if (event.kind == PaperMakerEventKind::Fill) {
                emit_fill(event, it->second, trade_row);
                ++fills_;
            } else if (event.kind == PaperMakerEventKind::CancelRequested) {
                emit_order_state(event, it->second, "CANCEL_REQUESTED");
            } else if (event.kind == PaperMakerEventKind::Cancelled) {
                emit_order_state(event, it->second, "CANCELLED");
                terminalize(it->first, "CANCELLED");
            }
            if (event.kind == PaperMakerEventKind::Fill
                && event.order_state == pm::v7::OrderState::Filled) {
                terminalize(event.order_id, "FILLED");
            }
        }
        (void)market_id;
    }

    [[nodiscard]] json::object common_metadata(const OrderContext& context) const {
        json::object metadata;
        metadata["paper_exploration"] = true;
        metadata["economic_authority"] = "PAPER_EXPLORATION";
        metadata["execution_authority"] = "SIMULATED_PAPER_ONLY";
        metadata["coordinator_receipt"] = context.authorization.receipt;
        metadata["opportunity_replay_key"] = context.authorization.replay_key;
        metadata["opportunity_envelope"] = context.authorization.envelope;
        if (const auto* alpha = child_object(context.authorization.envelope, "execution_alpha")) {
            metadata["execution_alpha"] = *alpha;
        }
        return metadata;
    }

    void spool(json::object event) {
        const std::string record_id = text(find_value(event, "record_id"));
        if (record_id.empty()) throw std::runtime_error("spool record_id missing");
        const auto recorded = integer(find_value(event, "recorded_ts_ms"), wall_ms());
        const fs::path target = spool_dir_ /
            (std::to_string(recorded) + "." + record_id + ".json");
        atomic_write(target, json::serialize(event) + "\n");
        ++spooled_events_;
    }

    void emit_order_submitted(const PaperMakerEvent& event, const OrderContext& context) {
        const auto now = wall_ms();
        const auto exchange_ms = std::min<std::int64_t>(now, event.timestamp_ns > 0
            ? integer(find_value(context.authorization.envelope, "decision_receive_timestamp_ns"), now * 1'000'000) / 1'000'000
            : now);
        const std::string order_id = std::to_string(event.order_id);
        json::object row;
        row["schema_version"] = 1;
        row["event_type"] = "ORDER_SUBMITTED";
        row["strategy"] = "MICRO_MAKER_PRO";
        row["model_sha"] = options_.model_sha;
        row["paper_only"] = true;
        row["authenticated_execution"] = false;
        row["record_id"] = "maker-submit-" + order_id + "-" + hex64(fnv1a(context.authorization.replay_key));
        row["recorded_ts_ms"] = now;
        row["opportunity_id"] = context.authorization.replay_key;
        row["order_id"] = order_id;
        row["market_id"] = context.authorization.market_id;
        row["event_id"] = context.authorization.event_id;
        row["token_id"] = context.authorization.token_id;
        row["decision_ts_ms"] = now;
        row["exchange_ts_ms"] = std::max<std::int64_t>(1, exchange_ms);
        row["receive_ts_ms"] = now;
        row["book_snapshot_id"] = text(find_value(context.authorization.envelope, "source_snapshot_identity"));
        row["side"] = "BUY";
        row["queue_ahead"] = micro_to_shares(event.queue.ahead_expected_microunits);
        row["limit_price"] = context.authorization.limit_price;
        row["predicted_fill_probability"] = context.authorization.fill_probability;
        row["expected_ev"] = context.authorization.conservative_ev;
        row["intended_action"] = "MAKE";
        row["intended_size"] = context.authorization.quantity_shares;
        row["order_state"] = "LIVE";
        row["timeout_ms"] = context.authorization.horizon_ms;
        row["metadata"] = common_metadata(context);
        spool(std::move(row));
    }

    void emit_fill(
        const PaperMakerEvent& event, const OrderContext& context,
        const json::object* trade_row) {
        if (event.operational_fill_microunits <= 0 || trade_row == nullptr) return;
        const auto receive_ms = integer(find_value(*trade_row, "receive_wall_ms"), wall_ms());
        const auto exchange_ns = integer(find_value(*trade_row, "exchange_event_ns"));
        const auto recorded = std::max(wall_ms(), receive_ms);
        const std::string order_id = std::to_string(event.order_id);
        const std::string fill_id = order_id + ":" + std::to_string(event.trade_id);
        const double fill_price = static_cast<double>(event.price_tick)
            * static_cast<double>(event.tick_size_e4) / kPriceScaleE4;
        json::object row;
        row["schema_version"] = 1;
        row["event_type"] = "FILL";
        row["strategy"] = "MICRO_MAKER_PRO";
        row["model_sha"] = options_.model_sha;
        row["paper_only"] = true;
        row["authenticated_execution"] = false;
        row["record_id"] = "maker-fill-" + hex64(mix64(event.order_id ^ event.trade_id));
        row["recorded_ts_ms"] = recorded;
        row["opportunity_id"] = context.authorization.replay_key;
        row["order_id"] = order_id;
        row["fill_id"] = fill_id;
        row["market_id"] = context.authorization.market_id;
        row["event_id"] = context.authorization.event_id;
        row["token_id"] = context.authorization.token_id;
        row["exchange_ts_ms"] = std::max<std::int64_t>(1, exchange_ns / 1'000'000);
        row["receive_ts_ms"] = std::max<std::int64_t>(1, receive_ms);
        row["book_snapshot_id"] = "fillability-trade-" + std::to_string(event.trade_id);
        row["side"] = "BUY";
        row["limit_price"] = context.authorization.limit_price;
        row["fill_price"] = fill_price;
        row["predicted_fill_probability"] = context.authorization.fill_probability;
        row["expected_ev"] = context.authorization.conservative_ev;
        row["intended_action"] = "MAKE";
        row["intended_size"] = context.authorization.quantity_shares;
        row["filled_size"] = micro_to_shares(event.operational_fill_microunits);
        row["fee"] = 0.0;
        row["fee_rate"] = 0.0;
        row["fee_source"] = "POLYMARKET_MAKER_ZERO";
        auto metadata = common_metadata(context);
        metadata["queue_scenario"] = "PESSIMISTIC_OPERATIONAL";
        metadata["pessimistic_fill_size"] = micro_to_shares(event.pessimistic_fill_microunits);
        metadata["expected_fill_size"] = micro_to_shares(event.expected_fill_microunits);
        metadata["optimistic_fill_size"] = micro_to_shares(event.optimistic_fill_microunits);
        metadata["execution_outcome"] = static_cast<std::uint64_t>(event.execution_outcome);
        row["metadata"] = std::move(metadata);
        spool(std::move(row));
    }

    void emit_order_state(
        const PaperMakerEvent& event, const OrderContext& context,
        std::string_view state) {
        const auto now = wall_ms();
        json::object row;
        row["schema_version"] = 1;
        row["event_type"] = "ORDER_STATE";
        row["strategy"] = "MICRO_MAKER_PRO";
        row["model_sha"] = options_.model_sha;
        row["paper_only"] = true;
        row["authenticated_execution"] = false;
        row["record_id"] = "maker-state-" + std::to_string(event.order_id) + "-" +
            std::string(state) + "-" + std::to_string(++event_sequence_);
        row["recorded_ts_ms"] = now;
        row["opportunity_id"] = context.authorization.replay_key;
        row["order_id"] = std::to_string(event.order_id);
        row["market_id"] = context.authorization.market_id;
        row["event_id"] = context.authorization.event_id;
        row["token_id"] = context.authorization.token_id;
        row["side"] = "BUY";
        row["intended_action"] = "MAKE";
        row["order_state"] = state;
        if (state == "CANCELLED") row["cancel_reason"] = "PAPER_ECONOMIC_HORIZON_OR_AUTHORITY_TERMINAL";
        row["metadata"] = common_metadata(context);
        spool(std::move(row));
    }

    void terminalize(std::uint64_t order_id, std::string_view reason) {
        auto it = orders_.find(order_id);
        if (it == orders_.end() || it->second.terminal) return;
        it->second.terminal = true;
        const std::string token = it->second.authorization.token_id;
        token_to_order_.erase(token);
        move_file(it->second.live_authorization_path, archive_dir_);
        ++terminal_orders_;
        last_terminal_reason_ = std::string(reason);
        orders_.erase(it);
    }

    void move_file(const fs::path& source, const fs::path& directory) {
        if (!fs::exists(source)) return;
        fs::create_directories(directory);
        const fs::path target = directory / source.filename();
        std::error_code error;
        fs::rename(source, target, error);
        if (error) {
            fs::copy_file(source, target, fs::copy_options::overwrite_existing, error);
            if (!error) fs::remove(source, error);
        }
    }

    void write_status() {
        json::object status;
        status["schema"] = "polymarket_v7_authorized_maker_paper_executor_status_v1";
        status["timestamp_ms"] = wall_ms();
        status["paper_only"] = true;
        status["authenticated_execution"] = false;
        status["real_order_submission"] = false;
        status["real_capital_at_risk"] = false;
        status["owner"] = "V7_GLOBAL_PORTFOLIO_COORDINATOR_RECEIPT_CONSUMER";
        status["execution_authority"] = "SIMULATED_PAPER_ONLY";
        status["model_sha"] = options_.model_sha;
        status["active_orders"] = static_cast<std::uint64_t>(orders_.size());
        status["submitted_orders"] = submitted_orders_;
        status["terminal_orders"] = terminal_orders_;
        status["fills"] = fills_;
        status["trade_rows_consumed"] = trade_rows_consumed_;
        status["invalid_trade_rows"] = invalid_trade_rows_;
        status["rejected_authorizations"] = rejected_authorizations_;
        status["restart_orphans"] = restart_orphans_;
        status["spooled_events"] = spooled_events_;
        status["last_error"] = last_error_;
        status["last_terminal_reason"] = last_terminal_reason_;
        status["queue_semantics"] = "MakerPaperMarketEngine_PESSIMISTIC_OPERATIONAL";
        status["trade_source"] = trade_tape_path_.string();
        atomic_write(status_path_, json::serialize(status) + "\n");
    }

    Options options_;
    fs::path authorization_dir_;
    fs::path live_dir_;
    fs::path archive_dir_;
    fs::path orphaned_dir_;
    fs::path rejected_dir_;
    fs::path selection_path_;
    fs::path fillability_status_path_;
    fs::path trade_tape_path_;
    fs::path status_path_;
    fs::path spool_dir_;
    std::ifstream trade_tape_;
    std::streamoff trade_offset_ = -1;
    std::unordered_map<std::string, MarketRuntime> markets_;
    std::unordered_map<std::uint64_t, OrderContext> orders_;
    std::unordered_map<std::string, std::uint64_t> token_to_order_;
    std::uint64_t submitted_orders_ = 0;
    std::uint64_t terminal_orders_ = 0;
    std::uint64_t fills_ = 0;
    std::uint64_t trade_rows_consumed_ = 0;
    std::uint64_t invalid_trade_rows_ = 0;
    std::uint64_t rejected_authorizations_ = 0;
    std::uint64_t restart_orphans_ = 0;
    std::uint64_t spooled_events_ = 0;
    std::uint64_t event_sequence_ = 0;
    std::string last_error_;
    std::string last_terminal_reason_;
};

} // namespace

int main(int argc, char** argv) {
    try {
        const Options options = parse_options(argc, argv);
        Executor executor(options);
        do {
            executor.run_once();
            if (options.once) break;
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
        } while (true);
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "polymarket_v7_authorized_maker_paper_executor: " << error.what() << '\n';
        return 2;
    }
}
