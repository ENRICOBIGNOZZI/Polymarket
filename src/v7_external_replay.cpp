#include "pm/v7_external_replay.hpp"

#include "pm/v7_external_fair.hpp"

#include <algorithm>
#include <cstring>
#include <fstream>
#include <string>
#include <unordered_set>
#include <vector>

namespace pm::v7::external_fair {
namespace {

struct LoadedRecord {
    TapeRecord record{};
    std::uint32_t file_index = 0;
    std::uint32_t priority = 0;
};

[[nodiscard]] bool valid_sha(std::string_view sha) noexcept {
    if (sha.size() != 40) return false;
    for (const char ch : sha) {
        if (!((ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f'))) return false;
    }
    return true;
}

[[nodiscard]] std::uint32_t kind_priority(TapeRecordKind kind) noexcept {
    switch (kind) {
        case TapeRecordKind::OracleEvent: return 1;
        case TapeRecordKind::SettlementReference: return 2;
        case TapeRecordKind::ExternalVenueEvent: return 3;
        case TapeRecordKind::PmState: return 4;
        case TapeRecordKind::PrivateState: return 5;
        case TapeRecordKind::InventoryState: return 6;
        case TapeRecordKind::RiskState: return 7;
        case TapeRecordKind::ExternalAssetSnapshot: return 8;
        case TapeRecordKind::FairValueSnapshot: return 9;
        case TapeRecordKind::CausalCut: return 10;
        case TapeRecordKind::CandidateAction: return 11;
        case TapeRecordKind::ExecutionEvent: return 12;
        case TapeRecordKind::TerminalSettlement: return 13;
        default: return 100;
    }
}

void fnv_mix(std::uint64_t& hash, const void* data, std::size_t size) noexcept {
    const auto* bytes = static_cast<const unsigned char*>(data);
    for (std::size_t i = 0; i < size; ++i) {
        hash ^= static_cast<std::uint64_t>(bytes[i]);
        hash *= 1099511628211ULL;
    }
}

[[nodiscard]] bool same_magic(const TapeSessionHeader& header) noexcept {
    static constexpr std::array<char, 8> expected{'P','M','V','7','T','A','P','E'};
    return header.magic == expected;
}

[[nodiscard]] std::string_view cstring_view(const std::array<char, 41>& value) noexcept {
    std::size_t size = 0;
    while (size < value.size() && value[size] != '\0') ++size;
    return {value.data(), size};
}

} // namespace

ExternalReplaySummary replay_external_tapes(
    std::span<const std::filesystem::path> paths,
    std::string_view expected_code_sha,
    std::uint64_t asset_handle,
    const ExternalStatePolicy& external_policy) noexcept {

    ExternalReplaySummary summary;
    if (paths.empty() || asset_handle == 0 || !valid_sha(expected_code_sha)) return summary;

    try {
        std::vector<LoadedRecord> records;
        for (std::size_t file_index = 0; file_index < paths.size(); ++file_index) {
            std::ifstream input(paths[file_index], std::ios::binary);
            if (!input) {
                ++summary.header_failures;
                continue;
            }
            TapeSessionHeader header;
            input.read(reinterpret_cast<char*>(&header), sizeof(header));
            if (!input || !same_magic(header)
                || header.schema_version != kExternalTapeSchemaVersion
                || header.record_bytes != sizeof(TapeRecord)
                || cstring_view(header.code_sha) != expected_code_sha) {
                ++summary.header_failures;
                continue;
            }

            std::uint64_t previous_sequence = 0;
            std::int64_t previous_receive = 0;
            while (true) {
                TapeRecord record;
                input.read(reinterpret_cast<char*>(&record), sizeof(record));
                if (input.eof()) {
                    if (input.gcount() != 0) ++summary.invalid_payloads;
                    break;
                }
                if (!input) {
                    ++summary.invalid_payloads;
                    break;
                }
                if (record.tape_sequence == 0 || record.receive_monotonic_ns <= 0
                    || record.payload_size == 0 || record.payload_size > record.payload.size()) {
                    ++summary.invalid_payloads;
                    continue;
                }
                if (previous_sequence != 0 && record.tape_sequence <= previous_sequence) {
                    ++summary.sequence_failures;
                }
                if (previous_receive != 0 && record.receive_monotonic_ns < previous_receive) {
                    ++summary.sequence_failures;
                }
                previous_sequence = record.tape_sequence;
                previous_receive = record.receive_monotonic_ns;
                records.push_back(LoadedRecord{
                    record,
                    static_cast<std::uint32_t>(file_index),
                    kind_priority(record.kind),
                });
            }
        }

        std::stable_sort(records.begin(), records.end(), [](const LoadedRecord& lhs,
                                                             const LoadedRecord& rhs) {
            if (lhs.record.receive_monotonic_ns != rhs.record.receive_monotonic_ns)
                return lhs.record.receive_monotonic_ns < rhs.record.receive_monotonic_ns;
            if (lhs.priority != rhs.priority) return lhs.priority < rhs.priority;
            if (lhs.record.source_handle != rhs.record.source_handle)
                return lhs.record.source_handle < rhs.record.source_handle;
            if (lhs.file_index != rhs.file_index) return lhs.file_index < rhs.file_index;
            return lhs.record.tape_sequence < rhs.record.tape_sequence;
        });

        OracleState oracle_state;
        ExternalAssetState external_state(asset_handle);
        std::unordered_set<std::uint64_t> observed_cuts;
        std::int64_t last_receive = 0;

        for (const auto& loaded : records) {
            const auto& record = loaded.record;
            ++summary.records;
            if (last_receive > record.receive_monotonic_ns) ++summary.causality_failures;
            last_receive = std::max(last_receive, record.receive_monotonic_ns);
            fnv_mix(summary.deterministic_hash, &record.tape_sequence, sizeof(record.tape_sequence));
            fnv_mix(summary.deterministic_hash, &record.receive_monotonic_ns, sizeof(record.receive_monotonic_ns));
            fnv_mix(summary.deterministic_hash, &record.source_handle, sizeof(record.source_handle));
            fnv_mix(summary.deterministic_hash, &record.kind, sizeof(record.kind));
            fnv_mix(summary.deterministic_hash, record.payload.data(), record.payload_size);

            switch (record.kind) {
                case TapeRecordKind::OracleEvent: {
                    ++summary.oracle_events;
                    OracleEvent event;
                    if (!decode_tape_payload(record, event)
                        || event.local_receive_monotonic_ns != record.receive_monotonic_ns
                        || event.local_receive_monotonic_ns <= 0
                        || !oracle_state.on_event(event)) {
                        ++summary.invalid_payloads;
                        break;
                    }
                    const auto snapshot = oracle_state.snapshot(
                        record.receive_monotonic_ns, 10'000'000'000LL);
                    external_state.on_oracle_snapshot(snapshot);
                    break;
                }
                case TapeRecordKind::ExternalVenueEvent: {
                    ++summary.external_venue_events;
                    ExternalVenueEvent event;
                    if (!decode_tape_payload(record, event)
                        || event.local_receive_monotonic_ns != record.receive_monotonic_ns
                        || !external_state.on_venue_event(event, external_policy)) {
                        ++summary.invalid_payloads;
                    }
                    break;
                }
                case TapeRecordKind::SettlementReference: {
                    ++summary.settlement_reference_events;
                    SettlementReferenceSnapshot reference;
                    if (!decode_tape_payload(record, reference)
                        || reference.reference_receive_monotonic_ns > record.receive_monotonic_ns
                        || reference.reference_receive_monotonic_ns <= 0) {
                        ++summary.causality_failures;
                    }
                    break;
                }
                case TapeRecordKind::FairValueSnapshot: {
                    ++summary.fair_snapshots;
                    FairValueSnapshot fair;
                    if (!decode_tape_payload(record, fair)
                        || fair.calculated_monotonic_ns > record.receive_monotonic_ns
                        || fair.valid_until_monotonic_ns < fair.calculated_monotonic_ns) {
                        ++summary.causality_failures;
                    }
                    break;
                }
                case TapeRecordKind::CausalCut: {
                    ++summary.causal_cuts;
                    CausalCut cut;
                    if (!decode_tape_payload(record, cut)
                        || !causal_cut_valid(cut)
                        || cut.decision_monotonic_ns > record.receive_monotonic_ns) {
                        ++summary.causality_failures;
                        break;
                    }
                    observed_cuts.insert(cut.causal_cut_id);
                    break;
                }
                case TapeRecordKind::CandidateAction: {
                    ++summary.candidate_actions;
                    CandidateAction action;
                    if (!decode_tape_payload(record, action)) {
                        ++summary.invalid_payloads;
                        break;
                    }
                    if (action.causal_cut_id == 0
                        || observed_cuts.find(action.causal_cut_id) == observed_cuts.end()) {
                        ++summary.action_without_causal_cut;
                        ++summary.causality_failures;
                    }
                    break;
                }
                case TapeRecordKind::PmState:
                case TapeRecordKind::PrivateState:
                case TapeRecordKind::InventoryState:
                case TapeRecordKind::RiskState:
                    ++summary.opaque_canonical_state_events;
                    break;
                case TapeRecordKind::ExternalAssetSnapshot:
                case TapeRecordKind::ExecutionEvent:
                case TapeRecordKind::TerminalSettlement:
                    // Preserved in deterministic ordering/hash. Their canonical
                    // owner-specific replay remains in the existing V7 execution
                    // subsystem; this bridge must not create duplicate truth.
                    break;
                default:
                    ++summary.invalid_payloads;
                    break;
            }
        }

        summary.final_oracle_state_version = oracle_state.state_version();
        summary.final_external_state_version = external_state.state_version();
        if (last_receive > 0) {
            summary.final_healthy_venues = external_state.snapshot(
                last_receive, external_policy).venue_count_fresh;
        }
        summary.valid = summary.records > 0
            && summary.header_failures == 0
            && summary.invalid_payloads == 0
            && summary.causality_failures == 0
            && summary.sequence_failures == 0
            && summary.action_without_causal_cut == 0 ? 1 : 0;
    } catch (...) {
        summary.valid = 0;
    }
    return summary;
}

} // namespace pm::v7::external_fair
