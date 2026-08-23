#include "pm/v4.hpp"

#include <filesystem>
#include <iostream>
#include <optional>
#include <stdexcept>
#include <string>

namespace {

void usage() {
    std::cout
        << "polymarket_v4_replay --config FILE --events FILE "
           "[--expected DECISIONS.jsonl] --run-dir DIR\n";
}

} // namespace

int main(int argc, char** argv) {
    try {
        std::string config_path = "config/v4.paper.example.json";
        std::optional<std::filesystem::path> events;
        std::optional<std::filesystem::path> expected;
        std::optional<std::string> run_dir;

        for (int i = 1; i < argc; ++i) {
            const std::string argument = argv[i];
            const auto next = [&]() {
                if (i + 1 >= argc) throw std::runtime_error("Missing value after " + argument);
                return std::string(argv[++i]);
            };
            if (argument == "--config") config_path = next();
            else if (argument == "--events") events = next();
            else if (argument == "--expected") expected = next();
            else if (argument == "--run-dir") run_dir = next();
            else if (argument == "--help" || argument == "-h") {
                usage();
                return 0;
            } else {
                throw std::runtime_error("Unknown argument: " + argument);
            }
        }
        if (!events) throw std::runtime_error("--events is required");
        if (!run_dir) throw std::runtime_error("--run-dir is required");

        auto config = pm::v4::V4Config::load(config_path);
        config.run_dir = *run_dir;
        config.scan_only = false;
        pm::v4::V4Engine engine(config);
        const auto result = engine.replay(*events, expected);
        std::cout << "replay ticks=" << result.ticks
                  << " expected=" << result.expected_hashes
                  << " mismatches=" << result.mismatches
                  << " final_equity=" << result.final_equity
                  << " final_hash=" << result.final_decision_hash
                  << '\n';
        return result.mismatches == 0 ? 0 : 2;
    } catch (const std::exception& error) {
        std::cerr << "fatal: " << error.what() << '\n';
        return 1;
    }
}
