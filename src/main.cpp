#include "poly/engine.hpp"

#include <cstdlib>
#include <iostream>
#include <string>

int main(int argc, char** argv) {
    try {
        poly::EngineConfig engine_cfg;
        poly::ClientConfig client_cfg;
        poly::ModelConfig model_cfg;
        poly::RiskConfig risk_cfg;

        int port = 8080;
        bool once = false;
        std::string web_root = "web";
        for (int i = 1; i < argc; ++i) {
            const std::string a = argv[i];
            if (a == "--port" && i + 1 < argc) port = std::stoi(argv[++i]);
            else if (a == "--cycle" && i + 1 < argc) engine_cfg.cycle_seconds = std::stoi(argv[++i]);
            else if (a == "--cash" && i + 1 < argc) engine_cfg.starting_cash = std::stod(argv[++i]);
            else if (a == "--web-root" && i + 1 < argc) web_root = argv[++i];
            else if (a == "--once") once = true;
            else if (a == "--max-markets" && i + 1 < argc) client_cfg.max_markets = std::stoull(argv[++i]);
            else if (a == "--min-liquidity" && i + 1 < argc) {
                const double x = std::stod(argv[++i]);
                client_cfg.min_liquidity_for_book = x;
                model_cfg.min_liquidity = x;
            }
        }

        std::cout << "Polymarket Quant Engine v0.2 -- PAPER TRADING ONLY\n";
        std::cout << "Hard drawdown limit: " << 100.0 * risk_cfg.max_drawdown << "%\n";
        poly::QuantEngine engine(engine_cfg, client_cfg, model_cfg, risk_cfg);
        if (once) {
            engine.run_cycle();
            std::cout << engine.state_json() << '\n';
            return 0;
        }
        engine.start();
        poly::serve_dashboard(engine, port, web_root);
    } catch (const std::exception& e) {
        std::cerr << "fatal: " << e.what() << '\n';
        return 1;
    }
    return 0;
}
