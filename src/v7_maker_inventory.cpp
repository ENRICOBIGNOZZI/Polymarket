#include "pm/v7_maker_paper.hpp"

#include <cmath>
#include <limits>

namespace pm::v7::maker {

PaperMakerResult MakerPaperMarketEngine::split_complete_sets(
    SleeveCapitalAccount& capital,
    std::int64_t microunits,
    std::int64_t timestamp_ns) noexcept {

    PaperMakerResult result;
    auto reject = [&]() noexcept {
        result.rejected = 1;
        PaperMakerEvent event;
        event.kind = PaperMakerEventKind::Rejected;
        event.timestamp_ns = timestamp_ns;
        event.original_microunits = microunits;
        emit(result, event);
        return result;
    };

    if (!policy_.valid() || microunits <= 0 || timestamp_ns <= 0) return reject();
    if (inventory_.yes_microunits > std::numeric_limits<std::int64_t>::max() - microunits
        || inventory_.no_microunits > std::numeric_limits<std::int64_t>::max() - microunits) {
        result.invariant_violation = 1;
        return reject();
    }

    constexpr double kMicrounitsPerShare = 1'000'000.0;
    const double split_shares = static_cast<double>(microunits) / kMicrounitsPerShare;
    const double leg_cost = 0.5 * split_shares;
    if (!std::isfinite(split_shares) || split_shares <= 0.0
        || !std::isfinite(leg_cost)
        || !std::isfinite(inventory_.yes_cost + leg_cost)
        || !std::isfinite(inventory_.no_cost + leg_cost)) {
        result.invariant_violation = 1;
        return reject();
    }

    // A complete-set share costs exactly one dollar of collateral.  Share
    // microunits and collateral microdollars therefore have the same integer
    // magnitude.  Fail closed before mutating inventory if the common V7
    // capital plane cannot reserve the transformation.
    if (!capital.commit_inventory(market_handle_, microunits)) return reject();

    inventory_.yes_microunits += microunits;
    inventory_.no_microunits += microunits;
    inventory_.yes_cost += leg_cost;
    inventory_.no_cost += leg_cost;

    PaperMakerEvent event;
    event.kind = PaperMakerEventKind::InventorySplit;
    event.timestamp_ns = timestamp_ns;
    event.original_microunits = microunits;
    event.operational_fill_microunits = microunits;
    event.realized_pnl = 0.0;
    emit(result, event);
    result.applied = 1;
    return result;
}

} // namespace pm::v7::maker
