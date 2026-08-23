#pragma once

#include "poly/model.hpp"
#include "poly/paper_broker.hpp"
#include "poly/polymarket_client.hpp"
#include "poly/risk.hpp"
#include <atomic>
#include <deque>
#include <mutex>
#include <string>
#include <thread>

namespace poly {

struct EngineConfig {
    int cycle_seconds{5};
    int rediscovery_cycles{60};
    double starting_cash{10000.0};
    bool paper_trading{true};
};

class QuantEngine {
public:
    QuantEngine(EngineConfig engine_cfg,
                ClientConfig client_cfg,
                ModelConfig model_cfg,
                RiskConfig risk_cfg);
    ~QuantEngine();

    void start();
    void stop();
    void run_cycle();

    [[nodiscard]] std::string state_json() const;
    [[nodiscard]] std::string signals_json() const;
    [[nodiscard]] std::string positions_json() const;
    [[nodiscard]] std::string equity_json() const;

private:
    EngineConfig engine_cfg_;
    PolymarketClient client_;
    StatisticalModel stat_model_;
    LogicalArbModel arb_model_;
    RiskEngine risk_;
    PaperBroker broker_;

    mutable std::mutex mu_;
    std::unordered_map<std::string, TokenMeta> meta_;
    std::unordered_map<std::string, BookSnapshot> books_;
    std::vector<Signal> signals_;
    std::deque<std::pair<long long, double>> equity_curve_;
    EngineMetrics metrics_;
    std::atomic<bool> running_{false};
    bool warm_started_{false};
    std::thread worker_;
};

void serve_dashboard(QuantEngine& engine, int port, const std::string& web_root);

} // namespace poly
