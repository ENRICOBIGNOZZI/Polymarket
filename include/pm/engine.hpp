#pragma once
#include "pm/api.hpp"
#include "pm/types.hpp"
#include <atomic>
#include <filesystem>
#include <unordered_set>

namespace pm {

class Engine {
public:
    explicit Engine(Config cfg);
    void run_once(bool paper, bool scan_only);
    void run_loop(bool paper);
    static Config load_config(const std::string& path);

    static std::optional<std::pair<double,double>> walk_book(const Book& book, bool buy, double requested_notional);
    static double protocol_fee(double shares, double price, const FeeDetails& fd);
    static double full_kelly(double q, double p);

private:
    Config cfg_;
    PolymarketApi api_;
    double cash_ = 0.0;
    double peak_equity_ = 0.0;
    bool killed_ = false;
    std::unordered_map<std::string,Position> positions_;
    std::unordered_map<std::string,std::deque<double>> history_;
    std::unordered_map<std::string,double> expert_brier_;
    std::unordered_map<std::string,double> expert_count_;
    std::unordered_map<std::string,std::unordered_map<std::string,double>> last_forecasts_;

    void ensure_runtime();
    void load_state();
    void persist_state(double equity, double gross);
    void append_signal(const Signal& s);
    void append_fill(const Fill& f);
    void append_history(std::int64_t ts, const Market& m, double mid);
    std::unordered_map<std::string,ExternalSignal> load_external() const;
    std::vector<ExpertPrediction> build_experts(const Market& m, const Book& yes,
                                                const std::vector<Market>& universe,
                                                const std::unordered_map<std::string,Book>& yes_books,
                                                const std::unordered_map<std::string,ExternalSignal>& external,
                                                const std::unordered_map<std::string,double>& pca_adjustment,
                                                const std::unordered_map<std::string,double>& graph_adjustment) const;
    std::unordered_map<std::string,double> pca_adjustments(const std::vector<Market>& markets,
                                                            const std::unordered_map<std::string,Book>& yes_books) const;
    std::unordered_map<std::string,double> graph_adjustments(const std::vector<Market>& markets,
                                                              const std::unordered_map<std::string,Book>& yes_books) const;
    std::pair<double,double> ensemble(const std::vector<ExpertPrediction>& preds, double spread) const;
    double equity(const std::unordered_map<std::string,Book>& books) const;
    double gross_exposure(const std::unordered_map<std::string,Book>& books) const;
    double open_loss_upper_bound() const;
    double market_exposure(const std::string& id) const;
    double event_exposure(const std::string& event) const;
    double size_trade(const Signal& s, const Market& m, double eq, double gross) const;
    void paper_trade(const Signal& s, const Market& m, const Book& side_book, const FeeDetails& fd, double notional);
    void maybe_exit(const Market& m, const Book& side_book, double fair_yes, const FeeDetails& fd);
    void score_resolved(const std::vector<Market>& markets);
    static std::string csv_escape(const std::string& s);
};

} // namespace pm
