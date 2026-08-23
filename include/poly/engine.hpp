#pragma once

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

namespace poly {

struct Level { double price{0.0}; double size{0.0}; };

struct BookSnapshot {
    std::string token_id;
    std::vector<Level> bids;
    std::vector<Level> asks;
    double best_bid{0.0};
    double best_ask{1.0};
    double bid_depth{0.0};
    double ask_depth{0.0};
    double midpoint() const { return 0.5 * (best_bid + best_ask); }
    double spread() const { return best_ask - best_bid; }
};

struct FeeInfo { double rate{0.0}; double exponent{1.0}; bool live{false}; };

struct Market {
    std::string id;
    std::string question;
    std::string slug;
    std::string condition_id;
    std::string event_id;
    std::string end_date;
    std::string yes_token;
    std::string no_token;
    bool active{false};
    bool closed{false};
    bool accepting_orders{false};
    bool neg_risk{false};
    double gamma_yes_price{0.5};
    double volume_24h{0.0};
    double liquidity{0.0};
    FeeInfo fees{};
};

struct LiveMarket { Market market; BookSnapshot yes_book; BookSnapshot no_book; std::vector<double> text_embedding; };
struct ExternalSignal { double probability{0.5}; double confidence{0.0}; std::string source; std::chrono::system_clock::time_point timestamp{}; };
struct ExpertPrediction { std::string expert; double probability{0.5}; double confidence{0.0}; bool active{false}; };
struct FairValue { double probability{0.5}; double uncertainty{0.5}; std::vector<ExpertPrediction> components; };

struct TradeIdea {
    std::string market_id;
    std::string question;
    std::string token_id;
    std::string outcome;
    double fair_probability{0.5};
    double entry_price{0.5};
    double raw_edge{0.0};
    double estimated_cost{0.0};
    double net_edge{0.0};
    double uncertainty{0.5};
    double desired_notional{0.0};
};

struct Position { std::string market_id; std::string token_id; std::string outcome; double shares{0.0}; double cost{0.0}; double fees_paid{0.0}; };
struct PaperFill { std::string action; std::string market_id; std::string token_id; std::string outcome; double shares{0.0}; double price{0.0}; double principal{0.0}; double fee{0.0}; double cash_after{0.0}; };

struct EngineConfig {
    std::size_t market_limit{100};
    std::size_t min_factor_history{20};
    std::size_t factor_history{180};
    double min_liquidity{250.0};
    double max_spread{0.12};
    double min_net_edge{0.015};
    double exit_edge{0.005};
    double uncertainty_buffer{0.35};
    double slippage_bps{5.0};
    double assumed_fee_bps{0.0};
    double fallback_taker_fee_rate{0.05};
    double fallback_taker_fee_exponent{1.0};
    double initial_capital{10000.0};
    double max_drawdown{0.15};
    double max_gross_fraction{0.65};
    double max_market_fraction{0.04};
    double max_event_fraction{0.12};
    double max_trade_fraction{0.02};
    double kelly_fraction{0.20};
    double ensemble_eta{2.0};
};

class HttpClient {
public:
    HttpClient();
    ~HttpClient();
    std::string get(const std::string& url) const;
    std::string post_json(const std::string& url, const std::string& body) const;
};

class PolymarketClient {
public:
    explicit PolymarketClient(HttpClient http = HttpClient{});
    std::vector<Market> discover_markets(std::size_t limit) const;
    Market get_market(const std::string& market_id) const;
    BookSnapshot get_book(const std::string& token_id) const;
    std::unordered_map<std::string, BookSnapshot> get_books(const std::vector<std::string>& token_ids) const;
    FeeInfo get_fee_info(const std::string& condition_id) const;
    std::vector<LiveMarket> snapshot(std::size_t limit, double min_liquidity, double max_spread) const;
private:
    HttpClient http_;
    mutable std::unordered_map<std::string, FeeInfo> fee_cache_;
};

class ExternalSignalStore {
public:
    void load_csv(const std::string& path);
    void set(const std::string& market_id, ExternalSignal signal);
    std::optional<ExternalSignal> get(const std::string& market_id) const;
private:
    std::unordered_map<std::string, ExternalSignal> signals_;
};

std::vector<double> hash_text_embedding(const std::string& text, std::size_t dim = 32);
double cosine_similarity(const std::vector<double>& a, const std::vector<double>& b);
double clamp_probability(double p);
double platform_fee_per_share(double price, double fee_rate, double fee_exponent);
std::vector<Market> parse_gamma_markets_json(const std::string& body);

class UniversalModel {
public:
    explicit UniversalModel(EngineConfig config);
    FairValue predict(const LiveMarket& market, const std::vector<LiveMarket>& universe, const ExternalSignalStore& external);
    void load_history(const std::string& path);
    void update_history(const std::vector<LiveMarket>& universe);
    std::size_t history_size(const std::string& market_id) const;
    void observe_resolution(const std::string& market_id, bool yes_outcome);
private:
    ExpertPrediction microstructure(const LiveMarket& m) const;
    ExpertPrediction factor(const LiveMarket& m, const std::vector<LiveMarket>& universe) const;
    ExpertPrediction graph(const LiveMarket& m, const std::vector<LiveMarket>& universe) const;
    ExpertPrediction semantic_relative_value(const LiveMarket& m, const std::vector<LiveMarket>& universe) const;
    ExpertPrediction external_signal(const LiveMarket& m, const ExternalSignalStore& external) const;
    double expert_weight(const ExpertPrediction& p) const;
    EngineConfig cfg_;
    std::string history_path_;
    std::unordered_map<std::string, std::deque<double>> price_history_;
    std::unordered_map<std::string, double> expert_brier_sum_;
    std::unordered_map<std::string, std::size_t> expert_brier_n_;
    std::unordered_map<std::string, FairValue> last_prediction_;
};

class RiskManager {
public:
    explicit RiskManager(EngineConfig config);
    bool killed() const { return killed_; }
    double peak_equity() const { return peak_equity_; }
    double drawdown(double equity) const;
    void update_equity(double equity);
    void load_state(const std::string& path);
    void save_state(const std::string& path) const;
    double allowed_notional(const TradeIdea& idea, double equity, double gross_exposure, double market_exposure, double event_exposure, double worst_case_open_loss) const;
private:
    EngineConfig cfg_;
    double peak_equity_{0.0};
    bool killed_{false};
};

class PaperBroker {
public:
    explicit PaperBroker(double cash);
    std::optional<PaperFill> buy(const TradeIdea& idea, double principal, double fee_rate, double fee_exponent, double slippage_bps);
    std::optional<PaperFill> close_token(const std::string& market_id, const std::string& token_id, const std::string& outcome, double best_bid, double fee_rate, double fee_exponent, double slippage_bps);
    std::vector<PaperFill> settle_market(const std::string& market_id, bool yes_outcome);
    void load_state(const std::string& path);
    void save_state(const std::string& path) const;
    double cash() const { return cash_; }
    double marked_equity(const std::unordered_map<std::string, double>& token_mid) const;
    double gross_value(const std::unordered_map<std::string, double>& token_mid) const;
    double worst_case_loss(const std::unordered_map<std::string, double>& token_mid) const;
    double market_exposure(const std::string& market_id, const std::unordered_map<std::string, double>& token_mid) const;
    double event_exposure(const std::string& event_id, const std::unordered_map<std::string, std::string>& market_to_event, const std::unordered_map<std::string, double>& token_mid) const;
    bool owns_token(const std::string& token_id) const;
    const std::vector<Position>& positions() const { return positions_; }
private:
    double cash_{0.0};
    std::vector<Position> positions_;
};

class QuantEngine {
public:
    explicit QuantEngine(EngineConfig config);
    void set_external_csv(const std::string& path);
    void run_once();
    double equity() const { return last_equity_; }
    const std::vector<TradeIdea>& last_ideas() const { return last_ideas_; }
private:
    TradeIdea make_idea(const LiveMarket& m, const FairValue& fair) const;
    void append_signal_log(const TradeIdea& idea, const FairValue& fair) const;
    void append_fill_log(const PaperFill& fill, const std::string& reason) const;
    void write_status(const std::vector<LiveMarket>& universe) const;
    EngineConfig cfg_;
    PolymarketClient client_;
    ExternalSignalStore external_;
    UniversalModel model_;
    RiskManager risk_;
    PaperBroker broker_;
    double last_equity_{0.0};
    std::vector<TradeIdea> last_ideas_;
};

} // namespace poly
