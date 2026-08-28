#include "pm/v7_external_fair.hpp"
#include "pm/v7_external_replay.hpp"
#include "pm/v7_external_tape.hpp"

#include <array>
#include <cassert>
#include <filesystem>
#include <string>

using namespace pm::v7::external_fair;

namespace {

const std::string kSha = "0123456789abcdef0123456789abcdef01234567";

OracleEvent oracle_event(std::uint64_t seq, std::int64_t receive) {
    OracleEvent event;
    event.feed_handle = 77;
    event.source_sequence = seq;
    event.connection_epoch = 1;
    event.exact_decimal_handle = 100 + seq;
    event.value_numeric = 65000.0 + static_cast<double>(seq);
    event.oracle_observation_ns = receive - 1;
    event.local_receive_monotonic_ns = receive;
    event.local_receive_wall_ns = receive + 1000;
    event.window_seconds = 60;
    event.healthy = 1;
    return event;
}

ExternalVenueEvent book(VenueId venue, std::uint64_t seq, std::int64_t receive,
                        double mid) {
    ExternalVenueEvent event;
    event.asset_handle = 500;
    event.source_sequence = seq;
    event.connection_epoch = 1;
    event.venue = venue;
    event.event_type = ExternalEventType::BookTop;
    event.local_receive_monotonic_ns = receive;
    event.local_receive_wall_ns = receive + 1000;
    event.bid = mid - 0.5;
    event.ask = mid + 0.5;
    event.bid_size = 2.0;
    event.ask_size = 2.0;
    event.healthy = 1;
    return event;
}

} // namespace

int main() {
    const auto root = std::filesystem::temp_directory_path() / "pm_v7_external_replay_test";
    std::filesystem::remove_all(root);
    std::filesystem::create_directories(root);
    const auto a_path = root / "a.bin";
    const auto b_path = root / "b.bin";

    {
        ExternalTapeRecorder a(a_path, kSha, "run", "oracle-session", "oracle", 1'000'000);
        assert(a.try_record(make_tape_record(
            TapeRecordKind::OracleEvent, 1, 100, 1, oracle_event(1, 100))));
        assert(a.try_record(make_tape_record(
            TapeRecordKind::OracleEvent, 2, 110, 1, oracle_event(2, 110))));

        SettlementReferenceSnapshot reference;
        reference.market_handle = 1;
        reference.contract_version = 1;
        reference.reference_version = 1;
        reference.reference_value_numeric = 65000.0;
        reference.reference_observation_ns = 80;
        reference.reference_receive_monotonic_ns = 90;
        reference.oracle_window_seconds = 60;
        reference.valid = 1;
        assert(a.try_record(make_tape_record(
            TapeRecordKind::SettlementReference, 3, 111, 2, reference)));
    }

    {
        ExternalTapeRecorder b(b_path, kSha, "run", "external-session", "external", 1'000'001);
        assert(b.try_record(make_tape_record(
            TapeRecordKind::ExternalVenueEvent, 1, 105, 10,
            book(VenueId::BinanceSpot, 1, 105, 65000.0))));
        assert(b.try_record(make_tape_record(
            TapeRecordKind::ExternalVenueEvent, 2, 106, 11,
            book(VenueId::CoinbaseSpot, 1, 106, 65001.0))));
        assert(b.try_record(make_tape_record(
            TapeRecordKind::ExternalVenueEvent, 3, 107, 12,
            book(VenueId::BybitSpot, 1, 107, 65002.0))));

        CausalCut cut;
        cut.causal_cut_id = 99;
        cut.decision_monotonic_ns = 120;
        cut.pm_state_version = 1;
        cut.oracle_state_version = 2;
        cut.external_state_version = 3;
        cut.settlement_reference_version = 1;
        cut.inventory_state_version = 1;
        cut.private_state_version = 1;
        cut.risk_state_version = 1;
        cut.max_input_receive_monotonic_ns = 119;
        cut.trigger_source = TriggerSource::ExternalPriceUpdate;
        cut.trigger_source_version = 3;
        cut.valid = 1;
        assert(b.try_record(make_tape_record(
            TapeRecordKind::CausalCut, 4, 120, 20, cut)));

        CandidateAction action;
        action.action = Action::Cancel;
        action.purpose = Purpose::Risk;
        action.market_handle = 1;
        action.instrument_handle = 2;
        action.causal_cut_id = 99;
        action.admissible = 1;
        action.critical = 1;
        assert(b.try_record(make_tape_record(
            TapeRecordKind::CandidateAction, 5, 121, 20, action)));
    }

    ExternalStatePolicy policy;
    policy.min_healthy_venues = 2;
    policy.max_venue_age_ns = 1'000;
    const std::array paths{a_path, b_path};
    const auto first = replay_external_tapes(paths, kSha, 500, policy);
    assert(first.valid == 1);
    assert(first.oracle_events == 2);
    assert(first.external_venue_events == 3);
    assert(first.causal_cuts == 1);
    assert(first.candidate_actions == 1);
    assert(first.final_healthy_venues == 3);
    assert(first.causality_failures == 0);

    const std::array reversed{b_path, a_path};
    const auto second = replay_external_tapes(reversed, kSha, 500, policy);
    assert(second.valid == 1);
    assert(second.deterministic_hash == first.deterministic_hash);

    const auto wrong_sha = replay_external_tapes(paths, std::string(40, 'f'), 500, policy);
    assert(wrong_sha.valid == 0);
    assert(wrong_sha.header_failures == 2);

    std::filesystem::remove_all(root);
    return 0;
}
