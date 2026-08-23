#pragma once

#include "pm/api.hpp"
#include "pm/types.hpp"

#include <array>
#include <cstdint>
#include <deque>
#include <filesystem>
#include <map>
#include <optional>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

namespace pm::v4 {

enum class Sleeve {
    Structural,
    Terminal,
    RelativeValue,
    Maker
};

std::string to_string(Sleeve sleeve);
Sleeve sleeve_from_string(const std::string& value);

struct V4Config {
    std::string gamma_url = "https://gamma-api.polymarket.com";
    std::string clob_url = "https://clob.polymarket.com";
    std::string run_dir = "runs/v4_paper";
    std::string external_signals_file = "data/external_signals.csv";
    std::string relations_file = "data/v4_relations.csv";

    std::size_t market_limit = 1200;
    std::size_t books_batch_size = 80;
    int interval_seconds = 5;
    double starting_capital = 10000.0;
    double min_liquidity = 50.0;
    double min_mid = 0.005;
    double max_mid = 0.995;
    double max_spread = 0.20;

    double max_trade_usd = 100.0;
    double max_market_fraction = 0.02;
    double max_event_fraction = 0.06;
    double max_gross_fraction = 0.20;
    double max_drawdown = 0.15;

    double terminal_min_edge = 0.0125;
    double terminal_min_confidence = 0.55;
    double rv_min_edge = 0.0075;
    double rv_z_threshold = 2.0;
    double rv_min_mr_probability = 0.68;
    double rv_reversion_strength = 0.30;
    std::size_t history_window = 240;
    std::size_t rv_min_history = 24;

    double structural_min_edge = 0.0025;
    double structural_completeness_tolerance = 0.08;

    bool maker_enabled = true;
    double maker_min_expected_edge = 0.0005;
    double maker_rebate_per_share = 0.0;
    double maker_liquidity_reward_per_share = 0.0;
    double maker_horizon_seconds = 60.0;
    double maker_min_fill_probability = 0.05;
    int maker_latency_ms = 250;
    double maker_max_order_usd = 50.0;

    double taker_slippage_bps = 5.0;
    double uncertainty_penalty = 0.20;
    bool scan_only = false;

    static V4Config load(const std::string& path);
    [[nodiscard]] pm::Config api_config() const;
};

struct SideBook {
    std::vector<pm::Level> bids;
    std::vector<pm::Level> asks;
    double tick_size = 0.01;
    double min_order_size = 1.0;
    double last_trade = 0.0;

    [[nodiscard]] double best_bid() const;
    [[nodiscard]] double best_ask() const;
    [[nodiscard]] double midpoint() const;
    [[nodiscard]] double spread() const;
    [[nodiscard]] double top_depth(bool bid_side, std::size_t levels = 3) const;
    [[nodiscard]] double microprice(std::size_t levels = 5) const;
};

struct MarketState {
    std::uint64_t sequence = 0;
    std::int64_t timestamp = 0;
    std::string market_id;
    std::string condition_id;
    std::string event_id;
    std::string slug;
    std::string question;
    std::string yes_token;
    std::string no_token;
    double liquidity = 0.0;
    double volume24h = 0.0;
    bool neg_risk = false;
    bool closed = false;
    std::optional<int> resolved_yes;
    pm::FeeDetails fee;
    SideBook yes;
    SideBook no;
    std::optional<double> external_q_yes;
    double external_confidence = 0.0;
    std::string external_source;
};

struct ExecutionLeg {
    std::string market_id;
    std::string side;
    std::string token_id;
    double price = 0.0;
    double quantity = 0.0;
};

struct Opportunity {
    std::string id;
    Sleeve sleeve = Sleeve::Terminal;
    std::string market_id;
    std::string event_id;
    std::string side;
    double fair_probability = 0.5;
    double executable_price = 0.5;
    double gross_edge = 0.0;
    double explicit_cost = 0.0;
    double uncertainty_cost = 0.0;
    double adverse_selection = 0.0;
    double fill_probability = 1.0;
    double expected_fill_seconds = 0.0;
    double expected_profit_per_unit = 0.0;
    double capital_per_unit = 0.0;
    double score = 0.0;
    double residual_z = 0.0;
    double mr_probability = 0.0;
    std::string reason;
    std::string state_hash;
    std::vector<ExecutionLeg> legs;
};

struct MakerOrder {
    std::string order_id;
    std::string opportunity_id;
    std::string market_id;
    std::string event_id;
    std::string side;
    std::string token_id;
    std::uint64_t created_sequence = 0;
    std::uint64_t active_sequence = 0;
    std::int64_t created_timestamp = 0;
    std::int64_t expires_timestamp = 0;
    double quote_price = 0.0;
    double original_quantity = 0.0;
    double remaining_quantity = 0.0;
    double queue_ahead = 0.0;
    double fill_probability = 0.0;
    double predicted_adverse_selection = 0.0;
    double reference_mid = 0.0;
    double spread = 0.0;
    double momentum = 0.0;
    std::string feature_bucket;
};

struct PaperPosition {
    std::string key;
    std::string market_id;
    std::string event_id;
    std::string side;
    std::string token_id;
    double shares = 0.0;
    double average_price = 0.0;
    double cost_basis = 0.0;
    double fees_paid = 0.0;
};

struct Fill {
    std::string fill_id;
    std::string order_id;
    std::string opportunity_id;
    Sleeve sleeve = Sleeve::Terminal;
    std::string action = "BUY";
    std::uint64_t sequence = 0;
    std::int64_t timestamp = 0;
    std::string market_id;
    std::string event_id;
    std::string side;
    std::string token_id;
    double quantity = 0.0;
    double price = 0.0;
    double fee = 0.0;
    bool maker = false;
    double spread = 0.0;
    double momentum = 0.0;
    double fill_probability = 1.0;
    std::string feature_bucket;
};

struct Markout {
    std::string fill_id;
    std::string market_id;
    std::string side;
    int horizon_seconds = 0;
    double fill_price = 0.0;
    double reference_mid = 0.0;
    double markout = 0.0;
    std::string feature_bucket;
};

struct TickDecision {
    std::uint64_t sequence = 0;
    std::int64_t timestamp = 0;
    std::string input_hash;
    std::string decision_hash;
    std::vector<Opportunity> opportunities;
    std::vector<std::string> actions;
};

struct TickResult {
    TickDecision decision;
    std::size_t markets = 0;
    std::size_t open_orders = 0;
    std::size_t open_positions = 0;
    double cash = 0.0;
    double equity = 0.0;
    double gross_exposure = 0.0;
    double drawdown = 0.0;
    bool killed = false;
};

struct ReplayResult {
    std::size_t ticks = 0;
    std::size_t expected_hashes = 0;
    std::size_t mismatches = 0;
    std::string final_decision_hash;
    double final_equity = 0.0;
};

class RegimeDetector {
public:
    static double mean_reversion_probability(const std::deque<double>& prices);
};

class AdverseSelectionModel {
public:
    double predict(const std::string& bucket, int horizon_seconds = 30) const;
    void update(const Markout& observation);
    void load(const std::filesystem::path& path);
    void save(const std::filesystem::path& path) const;

private:
    struct Cell {
        double mean = 0.0;
        double count = 0.0;
    };
    std::map<std::pair<std::string,int>, Cell> cells_;
};

class Recorder {
public:
    explicit Recorder(std::filesystem::path run_dir);

    void append_states(const std::vector<MarketState>& states);
    void append_decision(const TickDecision& decision);
    void append_order(const MakerOrder& order, const std::string& action);
    void append_fill(const Fill& fill);
    void append_markout(const Markout& markout);
    void write_status(const TickResult& result) const;

    static std::vector<MarketState> read_states(const std::filesystem::path& events_path);
    static std::unordered_map<std::uint64_t,std::string> read_expected_hashes(
        const std::filesystem::path& decisions_path);

private:
    std::filesystem::path run_dir_;
};

class V4Engine {
public:
    explicit V4Engine(V4Config config);

    TickResult run_live_once(bool paper, bool scan_only);
    void run_live_loop(bool paper);
    ReplayResult replay(const std::filesystem::path& events_path,
                        const std::optional<std::filesystem::path>& expected_decisions);

    static std::string canonical_state_hash(const std::vector<MarketState>& states);
    static std::string canonical_decision_hash(const TickDecision& decision);
    static std::vector<Opportunity> structural_opportunities(
        const std::vector<MarketState>& states,
        const V4Config& config);
    static double maker_fill_probability(const MarketState& state,
                                         const std::string& side,
                                         double momentum);
    static double maker_score(double quote_price,
                              double informational_edge,
                              double rebate,
                              double liquidity_reward,
                              double adverse_selection,
                              double fill_probability,
                              double expected_fill_seconds);
    static std::string feature_bucket(const MarketState& state,
                                      const std::string& side,
                                      double momentum);

private:
    V4Config config_;
    pm::PolymarketApi api_;
    Recorder recorder_;
    AdverseSelectionModel adverse_model_;

    std::uint64_t next_sequence_ = 1;
    double cash_ = 0.0;
    double peak_equity_ = 0.0;
    bool killed_ = false;

    std::unordered_map<std::string,std::deque<double>> price_history_;
    std::unordered_map<std::string,std::deque<double>> residual_history_;
    std::unordered_map<std::string,PaperPosition> positions_;
    std::unordered_map<std::string,MakerOrder> maker_orders_;
    std::unordered_map<std::string,pm::FeeDetails> fee_cache_;
    std::vector<Fill> pending_markouts_;
    std::unordered_map<std::string,std::uint32_t> markout_mask_;

    std::vector<MarketState> normalize_live_state(const std::vector<pm::Market>& markets,
                                                  const std::unordered_map<std::string,pm::Book>& books,
                                                  std::uint64_t sequence,
                                                  std::int64_t timestamp);
    TickResult process_tick(std::vector<MarketState> states,
                            bool paper,
                            bool scan_only,
                            bool record);
    TickDecision evaluate(const std::vector<MarketState>& states);
    std::vector<Opportunity> terminal_opportunities(const std::vector<MarketState>& states) const;
    std::vector<Opportunity> relative_value_opportunities(const std::vector<MarketState>& states);
    std::vector<Opportunity> maker_opportunities(const std::vector<MarketState>& states) const;

    void update_history(const std::vector<MarketState>& states);
    void process_maker_orders(const std::vector<MarketState>& states,
                              std::uint64_t sequence,
                              std::int64_t timestamp,
                              bool paper,
                              std::vector<std::string>& actions);
    void process_markouts(const std::vector<MarketState>& states);
    void allocate_and_execute(const std::vector<MarketState>& states,
                              const std::vector<Opportunity>& opportunities,
                              bool paper,
                              bool scan_only,
                              std::vector<std::string>& actions);
    void execute_taker(const Opportunity& opportunity,
                       const std::vector<MarketState>& states,
                       double notional,
                       std::vector<std::string>& actions);
    void place_maker_order(const Opportunity& opportunity,
                           const MarketState& state,
                           double notional,
                           std::vector<std::string>& actions);
    void apply_fill(const Fill& fill);
    void settle_positions(const std::vector<MarketState>& states,
                          std::vector<std::string>& actions);

    [[nodiscard]] double equity(const std::vector<MarketState>& states) const;
    [[nodiscard]] double gross_exposure() const;
    [[nodiscard]] double market_exposure(const std::string& market_id) const;
    [[nodiscard]] double event_exposure(const std::string& event_id) const;
    [[nodiscard]] double remaining_risk_capacity(double current_equity) const;

    void load_state();
    void save_state(const TickResult& result) const;
};

} // namespace pm::v4
