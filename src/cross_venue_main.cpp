#include "pm/cross_venue.hpp"

#include <filesystem>
#include <iostream>
#include <stdexcept>
#include <string>

namespace {

void usage() {
    std::cout << "usage: prediction_cross_venue_arb [--config PATH] [--run-dir PATH] "
                 "[--pairs PATH] [--credentials PATH] [--once|--loop]\n";
}

} // namespace

int main(int argc, char** argv) {
    try {
        std::filesystem::path config_path = "config/cross_venue.json";
        std::filesystem::path run_dir_override;
        std::filesystem::path pairs_override;
        std::filesystem::path credentials_override;
        bool loop = false;
        for (int i = 1; i < argc; ++i) {
            const std::string argument = argv[i];
            auto require_value = [&](const char* name) -> std::string {
                if (i + 1 >= argc) throw std::runtime_error(std::string("missing value for ") + name);
                return argv[++i];
            };
            if (argument == "--config") config_path = require_value("--config");
            else if (argument == "--run-dir") run_dir_override = require_value("--run-dir");
            else if (argument == "--pairs") pairs_override = require_value("--pairs");
            else if (argument == "--credentials") credentials_override = require_value("--credentials");
            else if (argument == "--loop") loop = true;
            else if (argument == "--once") loop = false;
            else if (argument == "--help" || argument == "-h") {
                usage();
                return 0;
            } else {
                throw std::runtime_error("unknown argument: " + argument);
            }
        }
        auto config = pm::cross::load_engine_config(config_path);
        if (!run_dir_override.empty()) config.run_dir = run_dir_override;
        if (!pairs_override.empty()) config.pair_manifest = pairs_override;
        if (!credentials_override.empty()) config.credentials_file = credentials_override;
        pm::cross::CrossVenueEngine engine(std::move(config));
        return loop ? engine.run_loop() : engine.run_once();
    } catch (const std::exception& error) {
        std::cerr << "fatal: " << error.what() << '\n';
        return 2;
    }
}
