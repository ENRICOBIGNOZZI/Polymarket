#include "pm/v4.hpp"

#include <iostream>
#include <optional>
#include <stdexcept>
#include <string>

namespace {

void usage() {
    std::cout
        << "polymarket_v4 --config FILE [--once|--loop] [--paper] [--scan-only] "
           "[--markets N] [--min-liquidity X] [--run-dir DIR]\n";
}

} // namespace

int main(int argc, char** argv) {
    try {
        std::string config_path = "config/v4.paper.example.json";
        bool loop = false;
        bool paper = false;
        bool scan_only = false;
        std::optional<std::size_t> markets;
        std::optional<double> min_liquidity;
        std::optional<std::string> run_dir;

        for (int i = 1; i < argc; ++i) {
            const std::string argument = argv[i];
            const auto next = [&]() {
                if (i + 1 >= argc) throw std::runtime_error("Missing value after " + argument);
                return std::string(argv[++i]);
            };
            if (argument == "--config") config_path = next();
            else if (argument == "--once") loop = false;
            else if (argument == "--loop") loop = true;
            else if (argument == "--paper") paper = true;
            else if (argument == "--scan-only") scan_only = true;
            else if (argument == "--markets") markets = static_cast<std::size_t>(std::stoull(next()));
            else if (argument == "--min-liquidity") min_liquidity = std::stod(next());
            else if (argument == "--run-dir") run_dir = next();
            else if (argument == "--help" || argument == "-h") {
                usage();
                return 0;
            } else {
                throw std::runtime_error("Unknown argument: " + argument);
            }
        }

        auto config = pm::v4::V4Config::load(config_path);
        if (markets) config.market_limit = *markets;
        if (min_liquidity) config.min_liquidity = *min_liquidity;
        if (run_dir) config.run_dir = *run_dir;
        if (scan_only) config.scan_only = true;

        pm::v4::V4Engine engine(config);
        if (loop) engine.run_live_loop(paper);
        else engine.run_live_once(paper, scan_only);
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "fatal: " << error.what() << '\n';
        return 1;
    }
}
