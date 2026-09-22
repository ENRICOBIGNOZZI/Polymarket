#include "pm/v7_native_settlement_authority.hpp"
#include "pm/v7_native_settlement_oms_endpoint.hpp"

#include <algorithm>
#include <cmath>
#include <limits>

namespace pm::v7 {
namespace {

constexpr std::int64_t kPriceScale = 10'000;

[[nodiscard]] bool terminal(OrderState state) noexcept {
    return state == OrderState::Filled || state == OrderState::Cancelled
        || state == OrderState::Rejected || state == OrderState::Expired
        || state == OrderState::Lost;
}

[[nodiscard]] bool mul_div_ceil_positive(std::int64_t lhs,
                                         std::int64_t rhs,
                                         std::int64_t divisor,
                                         std::int64_t& out) noexcept {
    out = 0;
    if (lhs <= 0 || rhs <= 0 || divisor <= 0) return false;
    const auto whole = lhs / divisor;
    const auto remainder = lhs % divisor;
    if (whole > std::numeric_limits<std::int64_t>::max() / rhs) return false;
    const auto base = whole * rhs;
    const auto remainder_product = remainder * rhs;
    const auto tail = remainder_product == 0
        ? 0 : 1 + (remainder_product - 1) / divisor;
    if (base > std::numeric_limits<std::int64_t>::max() - tail) return false;
    out = base + tail;
    return out > 0;
}

[[nodiscard]] std::int64_t proportional_basis_release(
    std::int64_t basis,
    std::int64_t total_microunits,
    std::int64_t fill_microunits) noexcept {
    if (basis <= 0 || total_microunits <= 0 || fill_microunits <= 0) return 0;
    if (fill_microunits >= total_microunits) return basis;
    const long double raw = static_cast<long double>(basis)
                          * static_cast<long double>(fill_microunits)
                          / static_cast<long double>(total_microunits);
    if (!std::isfinite(raw) || raw <= 0.0L) return 0;
    const auto released = static_cast<std::int64_t>(std::floor(raw));
    return std::clamp<std::int64_t>(released, 0, basis);
}

} // namespace

NativeSettlementAuthority::NativeSettlementAuthority(CapitalLimits limits) noexcept
    : capital_(limits) {}

std::size_t NativeSettlementAuthority::inventory_hash(
    std::uint64_t instrument_handle) const noexcept {
    instrument_handle ^= instrument_handle >> 33U;
    instrument_handle *= 0xff51afd7ed558ccdULL;
    instrument_handle ^= instrument_handle >> 33U;
    return static_cast<std::size_t>(instrument_handle) & (kNativeInventoryCapacity - 1);
}

NativeSettlementAuthority::InventorySlot* NativeSettlementAuthority::inventory(
    std::uint64_t instrument_handle) noexcept {
    if (instrument_handle == 0) return nullptr;
    const auto start = inventory_hash(instrument_handle);
    for (std::size_t probe = 0; probe < kNativeInventoryCapacity; ++probe) {
        auto& slot = inventory_[(start + probe) & (kNativeInventoryCapacity - 1)];
        if (slot.occupied == 0) return nullptr;
        if (slot.instrument_handle == instrument_handle) return &slot;
    }
    return nullptr;
}

const NativeSettlementAuthority::InventorySlot* NativeSettlementAuthority::inventory(
    std::uint64_t instrument_handle) const noexcept {
    if (instrument_handle == 0) return nullptr;
    const auto start = inventory_hash(instrument_handle);
    for (std::size_t probe = 0; probe < kNativeInventoryCapacity; ++probe) {
        const auto& slot = inventory_[(start + probe) & (kNativeInventoryCapacity - 1)];
        if (slot.occupied == 0) return nullptr;
        if (slot.instrument_handle == instrument_handle) return &slot;
    }
    return nullptr;
}

NativeSettlementAuthority::InventorySlot* NativeSettlementAuthority::ensure_inventory(
    std::uint64_t market_handle,
    std::uint64_t instrument_handle) noexcept {
    if (market_handle == 0 || instrument_handle == 0) return nullptr;
    const auto start = inventory_hash(instrument_handle);
    for (std::size_t probe = 0; probe < kNativeInventoryCapacity; ++probe) {
        auto& slot = inventory_[(start + probe) & (kNativeInventoryCapacity - 1)];
        if (slot.occupied != 0) {
            if (slot.instrument_handle == instrument_handle) {
                return slot.market_handle == market_handle ? &slot : nullptr;
            }
            continue;
        }
        slot = InventorySlot{};
        slot.market_handle = market_handle;
        slot.instrument_handle = instrument_handle;
        slot.occupied = 1;
        return &slot;
    }
    return nullptr;
}

bool NativeSettlementAuthority::instrument_has_active_order(
    std::uint64_t instrument_handle) const noexcept {
    for (const auto& item : order_tracks_) {
        if (item.occupied != 0 && item.instrument_handle == instrument_handle) return true;
    }
    return false;
}

NativeSettlementAuthority::OrderTrack* NativeSettlementAuthority::track(
    std::uint64_t client_order_id) noexcept {
    if (client_order_id == 0) return nullptr;
    auto& item = order_tracks_[static_cast<std::size_t>(client_order_id)
                              & (kNativeOrderTxCapacity - 1)];
    return item.occupied != 0 && item.client_order_id == client_order_id ? &item : nullptr;
}

const NativeSettlementAuthority::OrderTrack* NativeSettlementAuthority::track(
    std::uint64_t client_order_id) const noexcept {
    if (client_order_id == 0) return nullptr;
    const auto& item = order_tracks_[static_cast<std::size_t>(client_order_id)
                                    & (kNativeOrderTxCapacity - 1)];
    return item.occupied != 0 && item.client_order_id == client_order_id ? &item : nullptr;
}

void NativeSettlementAuthority::bump_inventory_version(InventorySlot& inv) noexcept {
    ++inv.state_version;
    if (inv.state_version == 0) ++inv.state_version;
}

bool NativeSettlementAuthority::sync_inventory(
    std::uint64_t market_handle,
    std::uint64_t instrument_handle,
    std::int64_t total_microunits,
    std::int64_t collateral_basis_microdollars,
    std::uint64_t state_version) noexcept {
    if (market_handle == 0 || instrument_handle == 0 || total_microunits < 0
        || collateral_basis_microdollars < 0 || state_version == 0
        || (total_microunits == 0 && collateral_basis_microdollars != 0)) {
        return false;
    }
    auto* inv = ensure_inventory(market_handle, instrument_handle);
    if (inv == nullptr || inv->reserved_sell_microunits != 0
        || instrument_has_active_order(instrument_handle)) {
        return false;
    }
    if (state_version < inv->state_version) return false;
    if (state_version == inv->state_version) {
        return inv->total_microunits == total_microunits
            && inv->collateral_basis_microdollars == collateral_basis_microdollars;
    }

    const auto old_basis = inv->collateral_basis_microdollars;
    if (collateral_basis_microdollars > old_basis) {
        if (!capital_.commit_inventory(
                market_handle, collateral_basis_microdollars - old_basis)) return false;
    } else if (collateral_basis_microdollars < old_basis) {
        if (!capital_.release_inventory(
                market_handle, old_basis - collateral_basis_microdollars)) return false;
    }
    inv->total_microunits = total_microunits;
    inv->collateral_basis_microdollars = collateral_basis_microdollars;
    inv->state_version = state_version;
    return true;
}

bool NativeSettlementAuthority::price_e4(
    const ExecutionPlan& plan,
    std::int64_t& out) const noexcept {
    out = 0;
    if (plan.intent.price_tick <= 0 || plan.tick_size_e4 <= 0
        || plan.tick_size_e4 >= kPriceScale) return false;
    const auto tick = static_cast<std::int64_t>(plan.tick_size_e4);
    if (plan.intent.price_tick > (kPriceScale - 1) / tick) return false;
    out = plan.intent.price_tick * tick;
    return out > 0 && out < kPriceScale;
}

bool NativeSettlementAuthority::cumulative_buy_basis(
    const OrderTrack& item,
    std::int64_t cumulative_fill_microunits,
    std::int64_t& out) const noexcept {
    return mul_div_ceil_positive(
        cumulative_fill_microunits, item.price_e4, kPriceScale, out);
}

NativeSettlementAuthorityResult NativeSettlementAuthority::submit(
    const ExecutionPlan& plan,
    std::int64_t minimum_order_microunits,
    std::int64_t now_monotonic_ns) noexcept {
    NativeSettlementAuthorityResult out;
    const auto& intent = plan.intent;
    if (minimum_order_microunits <= 0 || intent.quantity_microunits <= 0
        || plan.tick_size_e4 <= 0 || now_monotonic_ns <= 0) {
        out.reason = NativeSettlementAuthorityReason::InvalidPlan;
        return out;
    }
    if (intent.quantity_microunits < minimum_order_microunits) {
        out.reason = NativeSettlementAuthorityReason::BelowVenueMinimum;
        return out;
    }
    std::int64_t px_e4 = 0;
    if (!price_e4(plan, px_e4)) {
        out.reason = NativeSettlementAuthorityReason::InvalidPlan;
        return out;
    }

    auto* inv = ensure_inventory(intent.market_handle, intent.instrument_handle);
    if (inv == nullptr) {
        out.reason = NativeSettlementAuthorityReason::InventoryInvariant;
        return out;
    }

    const bool maker_quote = intent.strategy_id == StrategyId::ProfessionalMaker
        && intent.type == IntentType::Quote;
    if (maker_quote) {
        auto& active_client = intent.side == Side::Buy
            ? inv->maker_buy_client_order_id : inv->maker_sell_client_order_id;
        if (active_client != 0) {
            const auto* existing = order_tx_.find(active_client);
            if (existing == nullptr || terminal(existing->state)) {
                out.reason = NativeSettlementAuthorityReason::LifecycleInvariant;
                return out;
            }
            if (existing->price_tick == intent.price_tick
                && existing->original_microunits == intent.quantity_microunits) {
                out.reason = NativeSettlementAuthorityReason::DuplicateMakerQuote;
                return out;
            }
            out.cancel = order_tx_.prepare_cancel(active_client, now_monotonic_ns);
            out.reason = NativeSettlementAuthorityReason::MakerReplacePending;
            return out;
        }
    }

    if (intent.side == Side::Sell) {
        const auto available = inv->total_microunits - inv->reserved_sell_microunits;
        if (available < intent.quantity_microunits) {
            out.reason = NativeSettlementAuthorityReason::InventoryUnavailable;
            return out;
        }
        inv->reserved_sell_microunits += intent.quantity_microunits;
        bump_inventory_version(*inv);
    }

    out.admission = ExecutionAdmission::admit(intent, plan.tick_size_e4, capital_);
    if (out.admission.accepted == 0) {
        if (intent.side == Side::Sell) {
            inv->reserved_sell_microunits -= intent.quantity_microunits;
            bump_inventory_version(*inv);
        }
        out.reason = NativeSettlementAuthorityReason::AdmissionDenied;
        return out;
    }
    out.tx = order_tx_.prepare_submit(plan, now_monotonic_ns);
    if (out.tx.accepted == 0 || out.tx.oms.state != OrderState::SendPending) {
        if (out.admission.capital_reserved != 0) {
            out.capital_released_on_failure = capital_.release_order(intent.intent_id) ? 1 : 0;
        }
        if (intent.side == Side::Sell) {
            inv->reserved_sell_microunits -= intent.quantity_microunits;
            bump_inventory_version(*inv);
        }
        out.reason = NativeSettlementAuthorityReason::OmsDenied;
        return out;
    }

    auto& item = order_tracks_[static_cast<std::size_t>(out.tx.command.client_order_id)
                              & (kNativeOrderTxCapacity - 1)];
    if (item.occupied != 0) {
        OmsEvent reject;
        reject.type = OmsEventType::Reject;
        reject.timestamp_ns = now_monotonic_ns;
        (void)order_tx_.apply_owned(out.tx.command.client_order_id, reject);
        (void)order_tx_.retire_terminal(out.tx.command.client_order_id);
        if (out.admission.capital_reserved != 0) {
            out.capital_released_on_failure = capital_.release_order(intent.intent_id) ? 1 : 0;
        }
        if (intent.side == Side::Sell) {
            inv->reserved_sell_microunits -= intent.quantity_microunits;
            bump_inventory_version(*inv);
        }
        out.reason = NativeSettlementAuthorityReason::LifecycleInvariant;
        out.accepted = 0;
        return out;
    }

    item = OrderTrack{};
    item.admitted_command = out.tx.command;
    item.client_order_id = out.tx.command.client_order_id;
    item.intent_id = intent.intent_id;
    item.market_handle = intent.market_handle;
    item.instrument_handle = intent.instrument_handle;
    item.price_e4 = px_e4;
    item.reserved_sell_microunits = intent.side == Side::Sell
        ? intent.quantity_microunits : 0;
    item.side = intent.side;
    item.maker_quote = maker_quote ? 1 : 0;
    item.occupied = 1;
    if (maker_quote) {
        auto& active_client = intent.side == Side::Buy
            ? inv->maker_buy_client_order_id : inv->maker_sell_client_order_id;
        active_client = item.client_order_id;
    }

    out.reason = NativeSettlementAuthorityReason::Accepted;
    out.accepted = 1;
    return out;
}

NativeSettlementPairResult NativeSettlementAuthority::submit_pair(
    const ExecutionPlan& yes_plan,
    const ExecutionPlan& no_plan,
    std::int64_t minimum_order_microunits,
    std::int64_t now_monotonic_ns) noexcept {
    NativeSettlementPairResult out{};
    const auto& y = yes_plan.intent;
    const auto& n = no_plan.intent;
    const bool valid =
        minimum_order_microunits > 0 && now_monotonic_ns > 0
        && yes_plan.policy == ExecutionPolicyId::PureArbFok
        && no_plan.policy == ExecutionPolicyId::PureArbFok
        && y.strategy_id == StrategyId::HardArbitrage
        && n.strategy_id == StrategyId::HardArbitrage
        && y.type == IntentType::TargetPosition
        && n.type == IntentType::TargetPosition
        && y.market_handle != 0 && y.market_handle == n.market_handle
        && y.event_handle == n.event_handle
        && y.instrument_handle != 0 && n.instrument_handle != 0
        && y.instrument_handle != n.instrument_handle
        && y.intent_id != 0 && n.intent_id != 0 && y.intent_id != n.intent_id
        && y.quantity_microunits >= minimum_order_microunits
        && y.quantity_microunits == n.quantity_microunits
        && y.side == n.side && (y.side == Side::Buy || y.side == Side::Sell)
        && y.passive == 0 && n.passive == 0
        && y.post_only == 0 && n.post_only == 0
        && yes_plan.tick_size_e4 > 0 && no_plan.tick_size_e4 > 0;
    if (!valid) {
        out.reason = NativeSettlementPairReason::InvalidPair;
        return out;
    }

    out.yes = submit(yes_plan, minimum_order_microunits, now_monotonic_ns);
    if (out.yes.accepted == 0) {
        out.reason = NativeSettlementPairReason::FirstLegRejected;
        return out;
    }

    out.no = submit(no_plan, minimum_order_microunits, now_monotonic_ns);
    if (out.no.accepted == 0) {
        OmsEvent reject{};
        reject.type = OmsEventType::Reject;
        reject.timestamp_ns = now_monotonic_ns;
        out.rollback = apply_order_event(
            out.yes.tx.command.client_order_id, reject);
        const bool rolled_back =
            out.rollback.reason == NativeSettlementAuthorityReason::Accepted
            && out.rollback.transition.applied != 0
            && out.rollback.transition.state == OrderState::Rejected
            && out.rollback.terminal_retired != 0;
        out.rollback_complete = rolled_back ? 1 : 0;
        out.reason = rolled_back
            ? NativeSettlementPairReason::SecondLegRejectedRolledBack
            : NativeSettlementPairReason::RollbackFailed;
        return out;
    }

    out.risk_admitted_monotonic_ns = now_monotonic_ns;
    out.reason = NativeSettlementPairReason::Accepted;
    out.accepted = 1;
    return out;
}

NativeCancelTxResult NativeSettlementAuthority::cancel_maker_quote(
    std::uint64_t instrument_handle,
    Side side,
    std::int64_t now_monotonic_ns) noexcept {
    NativeCancelTxResult out;
    auto* inv = inventory(instrument_handle);
    if (inv == nullptr || (side != Side::Buy && side != Side::Sell)) return out;
    const auto client_order_id = side == Side::Buy
        ? inv->maker_buy_client_order_id : inv->maker_sell_client_order_id;
    if (client_order_id == 0) return out;
    return order_tx_.prepare_cancel(client_order_id, now_monotonic_ns);
}

bool NativeSettlementAuthority::apply_fill(
    OrderTrack& item,
    InventorySlot& inv,
    std::int64_t fill_delta_microunits,
    std::int64_t cumulative_fill_microunits,
    std::int32_t actual_fill_price_e4) noexcept {
    if (fill_delta_microunits <= 0 || cumulative_fill_microunits <= 0) return false;
    const bool actual_price_observed = actual_fill_price_e4 > 0;
    const auto fill_price = actual_price_observed
        ? static_cast<std::int64_t>(actual_fill_price_e4) : item.price_e4;
    if (fill_price <= 0 || fill_price >= kPriceScale
        || item.admitted_command.tick_size_e4 <= 0
        || fill_price % item.admitted_command.tick_size_e4 != 0
        || (item.side == Side::Buy && fill_price > item.price_e4)
        || (item.side == Side::Sell && fill_price < item.price_e4)) return false;
    if (item.side == Side::Buy) {
        std::int64_t delta_basis = 0;
        if (actual_price_observed) {
            // For an explicitly modelled/observed taker fill, move only the
            // dollars actually consumed at that arrival price. The unused
            // limit-price reservation remains until the terminal event.
            if (!mul_div_ceil_positive(
                    fill_delta_microunits, fill_price, kPriceScale, delta_basis)) return false;
        } else {
            // Preserve the original cumulative rounding semantics for legacy
            // and maker fills whose execution price is the admitted price.
            std::int64_t target_basis = 0;
            if (!cumulative_buy_basis(item, cumulative_fill_microunits, target_basis)
                || target_basis < item.settled_buy_basis_microdollars) return false;
            delta_basis = target_basis - item.settled_buy_basis_microdollars;
        }
        if (delta_basis > 0 && !capital_.settle_partial_fill(item.intent_id, delta_basis)) {
            return false;
        }
        if (inv.total_microunits > std::numeric_limits<std::int64_t>::max()
                                       - fill_delta_microunits
            || inv.collateral_basis_microdollars > std::numeric_limits<std::int64_t>::max()
                                                   - delta_basis
            || item.settled_buy_basis_microdollars > std::numeric_limits<std::int64_t>::max()
                                                   - delta_basis) return false;
        inv.total_microunits += fill_delta_microunits;
        inv.collateral_basis_microdollars += delta_basis;
        item.settled_buy_basis_microdollars += delta_basis;
        bump_inventory_version(inv);
        return true;
    }
    if (item.side != Side::Sell || item.reserved_sell_microunits < fill_delta_microunits
        || inv.reserved_sell_microunits < fill_delta_microunits
        || inv.total_microunits < fill_delta_microunits) return false;

    const auto release_basis = proportional_basis_release(
        inv.collateral_basis_microdollars, inv.total_microunits, fill_delta_microunits);
    if (release_basis > 0 && !capital_.release_inventory(inv.market_handle, release_basis)) {
        return false;
    }
    inv.total_microunits -= fill_delta_microunits;
    inv.reserved_sell_microunits -= fill_delta_microunits;
    inv.collateral_basis_microdollars -= release_basis;
    item.reserved_sell_microunits -= fill_delta_microunits;
    bump_inventory_version(inv);
    return true;
}

void NativeSettlementAuthority::clear_maker_quote(const OrderTrack& item) noexcept {
    if (item.maker_quote == 0) return;
    auto* inv = inventory(item.instrument_handle);
    if (inv == nullptr) return;
    auto& active = item.side == Side::Buy
        ? inv->maker_buy_client_order_id : inv->maker_sell_client_order_id;
    if (active == item.client_order_id) active = 0;
}

NativeLifecycleResult NativeSettlementAuthority::apply_order_event(
    std::uint64_t client_order_id,
    OmsEvent event) noexcept {
    NativeLifecycleResult out;
    auto* item = track(client_order_id);
    const auto* before = order_tx_.find(client_order_id);
    if (item == nullptr || before == nullptr || before->instrument_handle != item->instrument_handle
        || before->intent_id != item->intent_id) {
        out.reason = NativeSettlementAuthorityReason::LifecycleInvariant;
        return out;
    }
    auto* inv = inventory(item->instrument_handle);
    if (inv == nullptr || inv->market_handle != item->market_handle) {
        out.reason = NativeSettlementAuthorityReason::InventoryInvariant;
        return out;
    }
    const auto old_filled = before->filled_microunits;
    out.transition = order_tx_.apply_owned(client_order_id, event);
    const auto* after = order_tx_.find(client_order_id);
    if (after == nullptr) {
        out.reason = NativeSettlementAuthorityReason::LifecycleInvariant;
        return out;
    }
    out.record = *after;
    if (out.transition.invariant_violation != 0) {
        out.reason = NativeSettlementAuthorityReason::LifecycleInvariant;
        return out;
    }
    if (out.transition.applied != 0 && after->filled_microunits > old_filled) {
        const auto fill_delta = after->filled_microunits - old_filled;
        if (!apply_fill(*item, *inv, fill_delta, after->filled_microunits,
                        event.fill_price_e4)) {
            out.reason = NativeSettlementAuthorityReason::InventoryInvariant;
            return out;
        }
    }

    if (terminal(after->state)) {
        if (item->side == Side::Buy) {
            std::int64_t full_limit_basis = 0;
            if (!cumulative_buy_basis(*item, after->original_microunits, full_limit_basis)) {
                out.reason = NativeSettlementAuthorityReason::LifecycleInvariant;
                return out;
            }
            const bool residual_reservation =
                after->remaining_microunits > 0
                || item->settled_buy_basis_microdollars < full_limit_basis;
            if (residual_reservation && !capital_.release_order(item->intent_id)) {
                out.reason = NativeSettlementAuthorityReason::LifecycleInvariant;
                return out;
            }
        }
        if (item->side == Side::Sell && item->reserved_sell_microunits > 0) {
            if (inv->reserved_sell_microunits < item->reserved_sell_microunits) {
                out.reason = NativeSettlementAuthorityReason::InventoryInvariant;
                return out;
            }
            inv->reserved_sell_microunits -= item->reserved_sell_microunits;
            item->reserved_sell_microunits = 0;
            bump_inventory_version(*inv);
        }
        clear_maker_quote(*item);
        if (!order_tx_.retire_terminal(client_order_id)) {
            out.reason = NativeSettlementAuthorityReason::LifecycleInvariant;
            return out;
        }
        *item = OrderTrack{};
        out.terminal_retired = 1;
    }

    out.inventory = inventory_snapshot(inv->instrument_handle);
    out.reason = NativeSettlementAuthorityReason::Accepted;
    out.applied = out.transition.applied;
    return out;
}

bool NativeSettlementAuthority::matches_pending_command(
    const NativeOrderCommand& command) const noexcept {
    const auto* item = track(command.client_order_id);
    const auto* current = order_tx_.find(command.client_order_id);
    return item != nullptr && current != nullptr
        && current->state == OrderState::SendPending
        && command == item->admitted_command;
}

NativeInventorySnapshot NativeSettlementAuthority::inventory_snapshot(
    std::uint64_t instrument_handle) const noexcept {
    NativeInventorySnapshot out;
    const auto* inv = inventory(instrument_handle);
    if (inv == nullptr) return out;
    out.market_handle = inv->market_handle;
    out.instrument_handle = inv->instrument_handle;
    out.state_version = inv->state_version;
    out.total_microunits = inv->total_microunits;
    out.reserved_sell_microunits = inv->reserved_sell_microunits;
    out.available_microunits = std::max<std::int64_t>(
        0, inv->total_microunits - inv->reserved_sell_microunits);
    out.collateral_basis_microdollars = inv->collateral_basis_microdollars;
    return out;
}

// Settlement adapter endpoint is compiled with the authority itself so the
// London runtime cannot omit the sole lifecycle bridge translation unit.

const OmsOrderRecord* NativeSettlementOmsEndpoint::find(
    std::uint64_t client_order_id) const noexcept {
    if (const auto* current = authority_.find_order(client_order_id)) return current;
    return client_order_id != 0 && terminal_record_.client_order_id == client_order_id
        ? &terminal_record_ : nullptr;
}

OmsTransitionResult NativeSettlementOmsEndpoint::apply_owned(
    std::uint64_t client_order_id, OmsEvent event) noexcept {
    if (!healthy_) {
        OmsTransitionResult failed;
        failed.state = OrderState::Unknown;
        failed.invariant_violation = 1;
        failed.reconciliation_required = 1;
        return failed;
    }
    const auto result = authority_.apply_order_event(client_order_id, event);
    auto transition = result.transition;
    if (result.reason != NativeSettlementAuthorityReason::Accepted
        || transition.invariant_violation || transition.reconciliation_required) {
        healthy_ = false;
        transition.invariant_violation = 1;
        transition.reconciliation_required = 1;
        return transition;
    }
    if (result.applied) ++lifecycle_events_;
    if (result.terminal_retired) terminal_record_ = result.record;
    return transition;
}

bool NativeSettlementOmsEndpoint::observe_unsent(
    const NativeOrderCommand& command, std::int64_t now_monotonic_ns) noexcept {
    const auto* record = authority_.find_order(command.client_order_id);
    if (!matches_pending_command(command) || record == nullptr || record->state != OrderState::SendPending
        || command.command_id == 0 || command.client_order_id == 0
        || command.intent_id != record->intent_id
        || command.market_handle != record->market_handle
        || command.event_handle != record->event_handle
        || command.instrument_handle != record->instrument_handle
        || command.price_tick != record->price_tick
        || command.quantity_microunits != record->original_microunits
        || command.side != record->side || command.tick_size_e4 <= 0
        || command.decision_monotonic_ns != record->decision_monotonic_ns
        || command.queue_monotonic_ns != record->submission_ns
        || now_monotonic_ns < command.queue_monotonic_ns) {
        ++invalid_commands_;
        // Preserve the existing reservation for explicit reconciliation.
        healthy_ = false;
        return false;
    }
    OmsEvent rejected;
    rejected.type = OmsEventType::Reject;
    rejected.timestamp_ns = now_monotonic_ns;
    const auto result = apply_owned(command.client_order_id, rejected);
    if (!result.applied || result.state != OrderState::Rejected
        || result.invariant_violation || result.reconciliation_required) return false;
    ++observed_unsent_;
    return true;
}

} // namespace pm::v7
