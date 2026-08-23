#include "poly/engine.hpp"

#include <algorithm>
#include <chrono>
#include <filesystem>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>

int main(int argc, char** argv) {
    try {
        poly::EngineConfig cfg;
        bool once = true;
        bool fresh = false;
        int interval = 10;
        std::string external_csv;
        for (int i = 1; i < argc; ++i) {
            std::string a = argv[i];
            auto need = [&](const char* name) -> std::string {
                if (i + 1 >= argc) throw std::runtime_error(std::string("missing value for ") + name);
                return argv[++i];
            };
            if (a == "--loop") once = false;
            else if (a == "--once") once = true;
            else if (a == "--fresh") fresh = true;
            else if (a == "--interval") interval = std::stoi(need("--interval"));
            else if (a == "--markets") cfg.market_limit = std::stoul(need("--markets"));
            else if (a == "--capital") cfg.initial_capital = std::stod(need("--capital"));
            else if (a == "--min-edge") cfg.min_net_edge = std::stod(need("--min-edge"));
            else if (a == "--min-liquidity") cfg.min_liquidity = std::stod(need("--min-liquidity"));
            else if (a == "--max-spread") cfg.max_spread = std::stod(need("--max-spread"));
            else if (a == "--slippage-bps") cfg.slippage_bps = std::stod(need("--slippage-bps"));
            else if (a == "--max-drawdown") cfg.max_drawdown = std::stod(need("--max-drawdown"));
            else if (a == "--external-csv") external_csv = need("--external-csv");
            else if (a == "--help") {
                std::cout << "Usage: poly_live [--once|--loop] [--fresh] [--interval sec] [--markets N] "
                             "[--capital USD] [--min-edge p] [--min-liquidity USD] [--max-spread p] "
                             "[--slippage-bps bps] [--max-drawdown p<=0.15] [--external-csv file]\n";
                return 0;
            } else {
                throw std::runtime_error("unknown argument: " + a);
            }
        }

        if (cfg.market_limit == 0) throw std::runtime_error("--markets must be positive");
        if (cfg.initial_capital <= 0.0) throw std::runtime_error("--capital must be positive");
        if (cfg.max_drawdown <= 0.0 || cfg.max_drawdown > 0.15 + 1e-12)
            throw std::runtime_error("max drawdown guard must be in (0, 0.15]");
        if (cfg.max_spread <= 0.0 || cfg.max_spread >= 1.0) throw std::runtime_error("--max-spread must be in (0,1)");
        if (cfg.slippage_bps < 0.0) throw std::runtime_error("--slippage-bps may not be negative");
        if (fresh) std::filesystem::remove_all("runs");

        poly::QuantEngine engine(cfg);
        if (!external_csv.empty()) engine.set_external_csv(external_csv);
        do {
            engine.run_once();
            if (once) break;
            std::this_thread::sleep_for(std::chrono::seconds(std::max(1, interval)));
        } while (true);
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "fatal: " << e.what() << '\n';
        return 1;
    }
}
