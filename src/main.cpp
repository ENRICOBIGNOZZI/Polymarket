#include "poly/engine.hpp"

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <iostream>
#include <string>
#include <thread>

int main(int argc, char** argv) {
    try {
        poly::EngineConfig cfg;
        bool once = true;
        int interval = 10;
        std::string external_csv;
        for (int i=1;i<argc;++i) {
            std::string a=argv[i];
            auto need=[&](const char* name)->std::string { if(i+1>=argc) throw std::runtime_error(std::string("missing value for ")+name); return argv[++i]; };
            if(a=="--loop") once=false;
            else if(a=="--once") once=true;
            else if(a=="--interval") interval=std::stoi(need("--interval"));
            else if(a=="--markets") cfg.market_limit=std::stoul(need("--markets"));
            else if(a=="--capital") cfg.initial_capital=std::stod(need("--capital"));
            else if(a=="--min-edge") cfg.min_net_edge=std::stod(need("--min-edge"));
            else if(a=="--min-liquidity") cfg.min_liquidity=std::stod(need("--min-liquidity"));
            else if(a=="--external-csv") external_csv=need("--external-csv");
            else if(a=="--help") {
                std::cout << "Usage: poly_live [--once|--loop] [--interval sec] [--markets N] [--capital USD] [--min-edge p] [--min-liquidity USD] [--external-csv file]\n";
                return 0;
            } else throw std::runtime_error("unknown argument: "+a);
        }
        if (cfg.max_drawdown > 0.15 + 1e-12) throw std::runtime_error("max drawdown guard may not exceed 15%");
        poly::QuantEngine engine(cfg);
        if(!external_csv.empty()) engine.set_external_csv(external_csv);
        do {
            engine.run_once();
            if(once) break;
            std::this_thread::sleep_for(std::chrono::seconds(std::max(1,interval)));
        } while(true);
        return 0;
    } catch(const std::exception& e) {
        std::cerr << "fatal: " << e.what() << '\n';
        return 1;
    }
}
