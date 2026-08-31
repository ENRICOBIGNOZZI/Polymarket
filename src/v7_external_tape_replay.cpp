#include "pm/v7_external_replay.hpp"

#include <charconv>
#include <cstdint>
#include <filesystem>
#include <iostream>
#include <string>
#include <vector>

using namespace pm::v7::external_fair;

int main(int argc, char** argv) {
    std::vector<std::filesystem::path> tapes;
    std::string model_sha;
    std::uint64_t asset_handle = 0;
    for (int index = 1; index < argc; ++index) {
        const std::string argument = argv[index];
        if (argument == "--tape" && index + 1 < argc) tapes.emplace_back(argv[++index]);
        else if (argument == "--model-sha" && index + 1 < argc) model_sha = argv[++index];
        else if (argument == "--asset-handle" && index + 1 < argc) {
            const std::string text = argv[++index];
            const auto parsed = std::from_chars(text.data(), text.data() + text.size(), asset_handle);
            if (parsed.ec != std::errc{} || parsed.ptr != text.data() + text.size()) asset_handle = 0;
        } else {
            std::cerr << "usage: polymarket_v7_external_tape_replay --model-sha SHA --asset-handle N --tape PATH [--tape PATH ...]\n";
            return 2;
        }
    }
    if (model_sha.size() != 40 || asset_handle == 0 || tapes.empty()) {
        std::cerr << "model SHA, non-zero asset handle, and at least one tape are required\n";
        return 2;
    }
    const auto summary = replay_external_tapes(tapes, model_sha, asset_handle, ExternalStatePolicy{});
    std::cout << "{\"schema\":\"polymarket_v7_external_tape_replay_summary_v1\""
              << ",\"valid\":" << (summary.valid != 0 ? "true" : "false")
              << ",\"records\":" << summary.records
              << ",\"external_venue_events\":" << summary.external_venue_events
              << ",\"invalid_payloads\":" << summary.invalid_payloads
              << ",\"causality_failures\":" << summary.causality_failures
              << ",\"sequence_failures\":" << summary.sequence_failures
              << ",\"header_failures\":" << summary.header_failures
              << ",\"final_healthy_venues\":" << summary.final_healthy_venues
              << ",\"deterministic_hash\":" << summary.deterministic_hash << "}\n";
    return summary.valid != 0 ? 0 : 1;
}
