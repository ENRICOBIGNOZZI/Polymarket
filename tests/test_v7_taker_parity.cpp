#include "pm/v7_external_execution.hpp"

#include <boost/json.hpp>

#include <cassert>
#include <cmath>
#include <cstdint>
#include <fstream>
#include <iterator>
#include <string>

using namespace pm::v7;
using namespace pm::v7::external_fair;

namespace {

double number(const boost::json::value& value) {
    if (value.is_double()) return value.as_double();
    if (value.is_int64()) return static_cast<double>(value.as_int64());
    if (value.is_uint64()) return static_cast<double>(value.as_uint64());
    assert(false);
    return 0.0;
}

void close(double actual, double expected, double tolerance = 1e-10) {
    assert(std::isfinite(actual));
    assert(std::abs(actual - expected) <= tolerance);
}

FairValueSnapshot fair(std::int64_t now_ns) {
    FairValueSnapshot value;
    value.market_handle = 1;
    value.contract_version = 1;
    value.reference_version = 1;
    value.model_version = 3;
    value.model_hash_handle = 1;
    value.policy_version = 4;
    value.feature_schema_version = 1;
    value.horizon_seconds = 300;
    value.fair_yes = 0.85;
    value.fair_yes_lower = 0.80;
    value.fair_yes_upper = 0.90;
    value.structural_probability = 0.85;
    value.calibrated_probability = 0.85;
    value.calculated_monotonic_ns = now_ns;
    value.valid_until_monotonic_ns = now_ns + 1'000;
    value.contract_verified = 1;
    value.reference_verified = 1;
    value.model_mature = 1;
    value.oracle_healthy = 1;
    value.external_healthy = 1;
    value.valid = 1;
    return value;
}

} // namespace

int main(int argc, char** argv) {
    assert(argc == 2);
    std::ifstream input(argv[1]);
    assert(input.good());
    const std::string payload((std::istreambuf_iterator<char>(input)),
                              std::istreambuf_iterator<char>());
    const auto root = boost::json::parse(payload).as_object();
    assert(root.at("schema").as_string() == "polymarket_v7_taker_parity_fixture_v1");
    const double fee_rate = number(root.at("fee_rate"));
    const double fee_exponent = number(root.at("fee_exponent"));
    const double tick_size = number(root.at("tick_size"));

    std::uint64_t case_id = 0;
    for (const auto& raw_case : root.at("cases").as_array()) {
        const auto& test_case = raw_case.as_object();
        CandidateAction action;
        action.action = Action::Take;
        action.purpose = Purpose::Alpha;
        action.side = Side::Buy;
        action.market_handle = 1;
        action.instrument_handle = 3;
        action.causal_cut_id = 1;
        action.price_tick = static_cast<std::int64_t>(
            std::llround(number(test_case.at("limit_price")) / tick_size));
        action.quantity_microunits = static_cast<std::int64_t>(
            std::llround(number(test_case.at("quantity")) * 1'000'000.0));
        action.admissible = 1;

        const auto plan = build_execution_plan(
            action, 100 + case_id, 200 + case_id, 1, 3, 4, 99,
            static_cast<std::int32_t>(std::llround(tick_size * 10'000.0)));

        AggressiveBook book;
        book.pm_state_version = 40;
        book.receive_monotonic_ns = 1'005;
        const auto& asks = test_case.at("asks").as_array();
        assert(asks.size() <= book.asks.size());
        book.ask_count = static_cast<std::uint8_t>(asks.size());
        for (std::size_t index = 0; index < asks.size(); ++index) {
            const auto& level = asks[index].as_array();
            book.asks[index] = BookLevel{number(level[0]), number(level[1])};
        }
        book.valid = 1;

        FeeScheduleSnapshot fee;
        fee.market_handle = 1;
        fee.fee_version = 1;
        fee.rate = fee_rate;
        fee.exponent = fee_exponent;
        fee.taker_only = 1;
        fee.authoritative = 1;
        fee.valid = 1;

        const auto result = simulate_taker_paper(
            plan, book, fee, fair(1'000), true,
            AggressiveTimeInForce::Fak, 1'010);

        close(static_cast<double>(result.filled_microunits) / 1'000'000.0,
              number(test_case.at("expected_filled")));
        close(result.average_fill_price,
              number(test_case.at("expected_average_price")));
        close(result.gross_book_cost,
              number(test_case.at("expected_gross_cost")));
        close(result.authoritative_fee,
              number(test_case.at("expected_fee")));
        ++case_id;
    }
    return 0;
}
