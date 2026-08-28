#pragma once

#include "pm/v7_external_state.hpp"
#include "pm/v7_external_tape.hpp"

#include <cstdint>
#include <filesystem>
#include <span>
#include <string_view>
#include <type_traits>

namespace pm::v7::external_fair {

struct ExternalReplaySummary {
    std::uint64_t records = 0;
    std::uint64_t oracle_events = 0;
    std::uint64_t external_venue_events = 0;
    std::uint64_t settlement_reference_events = 0;
    std::uint64_t fair_snapshots = 0;
    std::uint64_t causal_cuts = 0;
    std::uint64_t candidate_actions = 0;
    std::uint64_t opaque_canonical_state_events = 0;
    std::uint64_t invalid_payloads = 0;
    std::uint64_t causality_failures = 0;
    std::uint64_t sequence_failures = 0;
    std::uint64_t header_failures = 0;
    std::uint64_t action_without_causal_cut = 0;
    std::uint64_t deterministic_hash = 1469598103934665603ULL;
    std::uint64_t final_oracle_state_version = 0;
    std::uint64_t final_external_state_version = 0;
    std::uint32_t final_healthy_venues = 0;
    std::uint8_t valid = 0;
    std::array<std::uint8_t, 3> reserved{};
};

// Offline only. Reads one or more source tapes, verifies exact SHA/schema and
// merges records by local receive time with a stable kind/source tie-break.
// Source/exchange timestamps never participate in executable ordering.
[[nodiscard]] ExternalReplaySummary replay_external_tapes(
    std::span<const std::filesystem::path> paths,
    std::string_view expected_code_sha,
    std::uint64_t asset_handle,
    const ExternalStatePolicy& external_policy) noexcept;

static_assert(std::is_trivially_copyable_v<ExternalReplaySummary>);

} // namespace pm::v7::external_fair
