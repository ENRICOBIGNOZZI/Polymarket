#include "pm/v7_execution_admission.hpp"

#include <cassert>

namespace {

pm::v7::CapitalLimits limits(std::int64_t total = 2'000'000) {
    pm::v7::CapitalLimits out;
    out.sleeve_budget_microdollars = total;
    out.max_total_exposure_microdollars = total;
    out.max_market_exposure_microdollars = total;
    out.max_single_order_microdollars = total;
    return out;
}

pm::v7::StrategyIntent quote(pm::v7::Side side,
                             std::uint64_t id = 1,
                             std::int64_t price_tick = 52,
                             std::int64_t quantity = 1'000'000) {
    pm::v7::StrategyIntent intent;
    intent.intent_id = id;
    intent.market_handle = 11;
    intent.event_handle = 22;
    intent.instrument_handle = 33;
    intent.strategy_id = pm::v7::StrategyId::ProfessionalMaker;
    intent.type = pm::v7::IntentType::Quote;
    intent.side = side;
    intent.price_tick = price_tick;
    intent.quantity_microunits = quantity;
    intent.passive = 1;
    intent.post_only = 1;
    return intent;
}

void test_buy_reserves_limit_notional() {
    pm::v7::SleeveCapitalAccount capital(limits());
    const auto intent = quote(pm::v7::Side::Buy);
    const auto result = pm::v7::ExecutionAdmission::admit(intent, 100, capital);
    assert(result.accepted);
    assert(result.capital_reserved);
    assert(result.reason == pm::v7::ExecutionAdmissionReason::Accepted);
    assert(result.reserved_microdollars == 520'000);
    assert(capital.snapshot().order_reserved_microdollars == 520'000);
}

void test_buy_rounds_reservation_up() {
    auto intent = quote(pm::v7::Side::Buy, 2, 1, 1);
    std::int64_t out = 0;
    assert(pm::v7::ExecutionAdmission::quote_buy_notional_microdollars(intent, 1, out));
    assert(out == 1);
}

void test_duplicate_buy_is_idempotent() {
    pm::v7::SleeveCapitalAccount capital(limits());
    const auto intent = quote(pm::v7::Side::Buy, 3);
    const auto first = pm::v7::ExecutionAdmission::admit(intent, 100, capital);
    const auto second = pm::v7::ExecutionAdmission::admit(intent, 100, capital);
    assert(first.accepted && second.accepted);
    assert(first.reserved_microdollars == second.reserved_microdollars);
    assert(capital.snapshot().order_reserved_microdollars == 520'000);
}

void test_buy_fails_closed_on_capital_limit() {
    pm::v7::SleeveCapitalAccount capital(limits(500'000));
    const auto result = pm::v7::ExecutionAdmission::admit(
        quote(pm::v7::Side::Buy, 4), 100, capital);
    assert(!result.accepted);
    assert(result.reason == pm::v7::ExecutionAdmissionReason::CapitalDenied);
    assert(capital.snapshot().total_exposure_microdollars == 0);
}

void test_sell_does_not_reserve_new_cash() {
    pm::v7::SleeveCapitalAccount capital(limits());
    const auto result = pm::v7::ExecutionAdmission::admit(
        quote(pm::v7::Side::Sell, 5), 100, capital);
    assert(result.accepted);
    assert(!result.capital_reserved);
    assert(capital.snapshot().total_exposure_microdollars == 0);
}

void test_control_traffic_bypasses_capital_pressure() {
    pm::v7::SleeveCapitalAccount capital(limits(500'000));
    assert(pm::v7::ExecutionAdmission::admit(
        quote(pm::v7::Side::Buy, 6, 50, 1'000'000), 100, capital).accepted);

    auto cancel = quote(pm::v7::Side::Buy, 7);
    cancel.type = pm::v7::IntentType::CancelQuote;
    cancel.quantity_microunits = 0;
    cancel.price_tick = 0;
    cancel.urgency = pm::v7::Urgency::Critical;
    const auto result = pm::v7::ExecutionAdmission::admit(cancel, 0, capital);
    assert(result.accepted);
    assert(result.reason == pm::v7::ExecutionAdmissionReason::ControlBypass);
    assert(!result.capital_reserved);
}

void test_unsupported_strategy_intent_fails_closed_until_planned() {
    pm::v7::SleeveCapitalAccount capital(limits());
    auto intent = quote(pm::v7::Side::Buy, 8);
    intent.type = pm::v7::IntentType::MultiLegTarget;
    const auto result = pm::v7::ExecutionAdmission::admit(intent, 100, capital);
    assert(!result.accepted);
    assert(result.reason == pm::v7::ExecutionAdmissionReason::UnsupportedIntent);
    assert(capital.snapshot().total_exposure_microdollars == 0);
}

void test_invalid_price_domain_rejected() {
    pm::v7::SleeveCapitalAccount capital(limits());
    const auto result = pm::v7::ExecutionAdmission::admit(
        quote(pm::v7::Side::Buy, 9, 100), 100, capital); // exactly $1.00
    assert(!result.accepted);
    assert(result.reason == pm::v7::ExecutionAdmissionReason::InvalidTick);
}

} // namespace

int main() {
    test_buy_reserves_limit_notional();
    test_buy_rounds_reservation_up();
    test_duplicate_buy_is_idempotent();
    test_buy_fails_closed_on_capital_limit();
    test_sell_does_not_reserve_new_cash();
    test_control_traffic_bypasses_capital_pressure();
    test_unsupported_strategy_intent_fails_closed_until_planned();
    test_invalid_price_domain_rejected();
    return 0;
}
