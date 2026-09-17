#include "pm/v7_user_oms_bridge.hpp"

#include <algorithm>
#include <array>
#include <limits>

namespace pm::v7 {
namespace {
constexpr std::size_t kPendingFillCapacity = 1024;
constexpr std::size_t kPendingLifecycleCapacity = 256;
constexpr std::size_t kDedupeBucketCount = 1024;
constexpr std::size_t kDedupeWays = 4;
constexpr std::int64_t kPendingTtlNs = 30'000'000'000LL;

[[nodiscard]] bool copy_id(clob_identity::FixedOrderId& output,
                           std::string_view input) noexcept {
    if (input.empty() || input.size() > output.bytes.size()) return false;
    std::copy(input.begin(), input.end(), output.bytes.begin());
    output.size = static_cast<std::uint16_t>(input.size());
    return true;
}

template <std::size_t N>
[[nodiscard]] bool copy_text(user_ws::FixedText<N>& output,
                             std::string_view input) noexcept {
    if (input.empty() || input.size() > output.data.size()) return false;
    std::copy(input.begin(), input.end(), output.data.begin());
    output.size = static_cast<std::uint16_t>(input.size());
    return true;
}

[[nodiscard]] bool parse_microunits(std::string_view value,
                                    std::int64_t& output) noexcept {
    if (value.empty()) return false;
    std::size_t position = 0;
    std::uint64_t whole = 0;
    bool any = false;
    while (position < value.size() && value[position] >= '0' && value[position] <= '9') {
        any = true;
        const std::uint64_t digit = static_cast<std::uint64_t>(value[position++] - '0');
        if (whole > (static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max())
                     - digit) / 10U) return false;
        whole = whole * 10U + digit;
    }
    if (!any) return false;
    std::uint64_t fraction = 0;
    unsigned fraction_digits = 0;
    if (position < value.size() && value[position] == '.') {
        ++position;
        while (position < value.size() && value[position] >= '0' && value[position] <= '9') {
            const unsigned digit = static_cast<unsigned>(value[position++] - '0');
            if (fraction_digits < 6U) {
                fraction = fraction * 10U + digit;
                ++fraction_digits;
            } else if (digit != 0U) {
                // Never round execution quantities silently.
                return false;
            }
        }
    }
    if (position != value.size()) return false;
    while (fraction_digits < 6U) {
        fraction *= 10U;
        ++fraction_digits;
    }
    constexpr std::uint64_t scale = 1'000'000ULL;
    if (whole > (static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max())
                 - fraction) / scale) return false;
    const auto scaled = whole * scale + fraction;
    if (scaled == 0 || scaled > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()))
        return false;
    output = static_cast<std::int64_t>(scaled);
    return true;
}

[[nodiscard]] std::uint64_t pair_hash(std::string_view trade_id,
                                      std::string_view order_id) noexcept {
    std::uint64_t hash = 14695981039346656037ULL;
    for (const unsigned char byte : trade_id) {
        hash ^= byte;
        hash *= 1099511628211ULL;
    }
    hash ^= 0xffU;
    hash *= 1099511628211ULL;
    for (const unsigned char byte : order_id) {
        hash ^= byte;
        hash *= 1099511628211ULL;
    }
    return hash;
}
} // namespace

struct UserOmsBridge::Impl {
    struct PendingFill {
        clob_identity::FixedOrderId exchange_order_id{};
        clob_identity::FixedOrderId trade_id{};
        user_ws::FixedText<40> amount{};
        std::int64_t receive_ns = 0;
    };
    struct PendingLifecycle {
        clob_identity::FixedOrderId exchange_order_id{};
        user_ws::OrderEventType type = user_ws::OrderEventType::Unknown;
        std::int64_t receive_ns = 0;
    };
    struct DedupeEntry {
        clob_identity::FixedOrderId exchange_order_id{};
        clob_identity::FixedOrderId trade_id{};
        std::uint8_t occupied = 0;
    };

    clob_identity::OrderIdentityMap identities{};
    std::array<PendingFill, kPendingFillCapacity> pending_fills{};
    std::array<PendingLifecycle, kPendingLifecycleCapacity> pending_lifecycle{};
    std::array<DedupeEntry, kDedupeBucketCount * kDedupeWays> dedupe{};
    std::size_t pending_fill_count = 0;
    std::size_t pending_lifecycle_count = 0;
    std::uint64_t next_event_id = 1;
    std::uint64_t routed_fills = 0;
    std::uint64_t duplicate_fills = 0;
    std::uint64_t foreign_correlations = 0;
    std::uint64_t pending_overflow = 0;
    std::uint64_t dedupe_bucket_overflow = 0;

    [[nodiscard]] std::uint64_t event_id() noexcept {
        const auto current = next_event_id++;
        if (next_event_id == 0) next_event_id = 1;
        return current == 0 ? event_id() : current;
    }

    [[nodiscard]] bool emit(std::uint64_t client_order_id,
                            OmsEventType type,
                            std::int64_t timestamp_ns,
                            std::int64_t fill_delta,
                            std::span<RoutedOmsEvent> output,
                            UserOmsBridgeResult& result) noexcept {
        if (result.output_count >= output.size()) {
            result.output_overflow = 1;
            return false;
        }
        auto& routed = output[result.output_count++];
        routed.client_order_id = client_order_id;
        routed.event = {};
        routed.event.event_id = event_id();
        routed.event.type = type;
        routed.event.timestamp_ns = timestamp_ns;
        routed.event.fill_delta_microunits = fill_delta;
        return true;
    }

    void prune_pending(std::int64_t now_ns) noexcept {
        if (now_ns <= 0) return;
        for (std::size_t i = 0; i < pending_fill_count;) {
            const auto age = now_ns - pending_fills[i].receive_ns;
            if (age > kPendingTtlNs) {
                ++foreign_correlations;
                pending_fills[i] = pending_fills[--pending_fill_count];
            } else {
                ++i;
            }
        }
        for (std::size_t i = 0; i < pending_lifecycle_count;) {
            const auto age = now_ns - pending_lifecycle[i].receive_ns;
            if (age > kPendingTtlNs) {
                ++foreign_correlations;
                pending_lifecycle[i] = pending_lifecycle[--pending_lifecycle_count];
            } else {
                ++i;
            }
        }
    }

    [[nodiscard]] bool dedupe_contains(std::string_view trade_id,
                                       std::string_view exchange_order_id) const noexcept {
        const std::size_t bucket = static_cast<std::size_t>(
            pair_hash(trade_id, exchange_order_id) & (kDedupeBucketCount - 1U));
        for (std::size_t way = 0; way < kDedupeWays; ++way) {
            const auto& entry = dedupe[bucket * kDedupeWays + way];
            if (entry.occupied && entry.trade_id.view() == trade_id
                && entry.exchange_order_id.view() == exchange_order_id) return true;
        }
        return false;
    }

    [[nodiscard]] bool dedupe_insert(std::string_view trade_id,
                                     std::string_view exchange_order_id) noexcept {
        const std::size_t bucket = static_cast<std::size_t>(
            pair_hash(trade_id, exchange_order_id) & (kDedupeBucketCount - 1U));
        for (std::size_t way = 0; way < kDedupeWays; ++way) {
            auto& entry = dedupe[bucket * kDedupeWays + way];
            if (entry.occupied && entry.trade_id.view() == trade_id
                && entry.exchange_order_id.view() == exchange_order_id) return true;
            if (!entry.occupied) {
                if (!copy_id(entry.trade_id, trade_id)
                    || !copy_id(entry.exchange_order_id, exchange_order_id)) return false;
                entry.occupied = 1;
                return true;
            }
        }
        ++dedupe_bucket_overflow;
        return false;
    }

    [[nodiscard]] bool pending_fill_exists(std::string_view trade_id,
                                           std::string_view order_id) const noexcept {
        for (std::size_t i = 0; i < pending_fill_count; ++i) {
            if (pending_fills[i].trade_id.view() == trade_id
                && pending_fills[i].exchange_order_id.view() == order_id) return true;
        }
        return false;
    }

    void pend_fill(std::string_view order_id,
                   std::string_view trade_id,
                   std::string_view amount,
                   std::int64_t receive_ns,
                   UserOmsBridgeResult& result) noexcept {
        if (pending_fill_exists(trade_id, order_id)) {
            result.duplicate_fill = 1;
            return;
        }
        if (pending_fill_count >= pending_fills.size()) {
            result.pending_overflow = 1;
            ++pending_overflow;
            return;
        }
        auto& pending = pending_fills[pending_fill_count];
        if (!copy_id(pending.exchange_order_id, order_id)
            || !copy_id(pending.trade_id, trade_id)
            || !copy_text(pending.amount, amount)) {
            result.pending_overflow = 1;
            ++pending_overflow;
            return;
        }
        pending.receive_ns = receive_ns;
        ++pending_fill_count;
    }

    void pend_lifecycle(std::string_view order_id,
                        user_ws::OrderEventType type,
                        std::int64_t receive_ns,
                        UserOmsBridgeResult& result) noexcept {
        for (std::size_t i = 0; i < pending_lifecycle_count; ++i) {
            if (pending_lifecycle[i].exchange_order_id.view() == order_id
                && pending_lifecycle[i].type == type) return;
        }
        if (pending_lifecycle_count >= pending_lifecycle.size()) {
            result.pending_overflow = 1;
            ++pending_overflow;
            return;
        }
        auto& pending = pending_lifecycle[pending_lifecycle_count];
        if (!copy_id(pending.exchange_order_id, order_id)) {
            result.pending_overflow = 1;
            ++pending_overflow;
            return;
        }
        pending.type = type;
        pending.receive_ns = receive_ns;
        ++pending_lifecycle_count;
    }

    void route_fill(std::string_view order_id,
                    std::string_view trade_id,
                    std::string_view amount,
                    std::int64_t receive_ns,
                    std::span<RoutedOmsEvent> output,
                    UserOmsBridgeResult& result) noexcept {
        const auto identity = identities.lookup_client(order_id);
        if (!identity.found) {
            pend_fill(order_id, trade_id, amount, receive_ns, result);
            return;
        }
        if (dedupe_contains(trade_id, order_id)) {
            result.duplicate_fill = 1;
            ++duplicate_fills;
            return;
        }
        std::int64_t microunits = 0;
        if (!parse_microunits(amount, microunits)) {
            result.pending_overflow = 1;
            ++pending_overflow;
            return;
        }
        if (result.output_count >= output.size()) {
            result.output_overflow = 1;
            return;
        }
        if (!dedupe_insert(trade_id, order_id)) {
            result.pending_overflow = 1;
            ++pending_overflow;
            return;
        }
        if (emit(identity.client_order_id, OmsEventType::FillDelta, receive_ns,
                 microunits, output, result)) ++routed_fills;
    }

    void route_lifecycle(std::string_view order_id,
                         user_ws::OrderEventType type,
                         std::int64_t receive_ns,
                         std::span<RoutedOmsEvent> output,
                         UserOmsBridgeResult& result) noexcept {
        const auto identity = identities.lookup_client(order_id);
        if (!identity.found) {
            if (type == user_ws::OrderEventType::Placement
                || type == user_ws::OrderEventType::Cancellation) {
                pend_lifecycle(order_id, type, receive_ns, result);
            }
            return;
        }
        if (type == user_ws::OrderEventType::Placement) {
            (void)emit(identity.client_order_id, OmsEventType::AckLive,
                       receive_ns, 0, output, result);
        } else if (type == user_ws::OrderEventType::Cancellation) {
            (void)emit(identity.client_order_id, OmsEventType::AckCancel,
                       receive_ns, 0, output, result);
        }
    }

    void flush_pending(std::string_view order_id,
                       std::span<RoutedOmsEvent> output,
                       UserOmsBridgeResult& result) noexcept {
        for (std::size_t i = 0; i < pending_lifecycle_count;) {
            if (pending_lifecycle[i].exchange_order_id.view() != order_id) {
                ++i;
                continue;
            }
            if (result.output_count >= output.size()) {
                result.output_overflow = 1;
                return;
            }
            const auto pending = pending_lifecycle[i];
            route_lifecycle(order_id, pending.type, pending.receive_ns, output, result);
            pending_lifecycle[i] = pending_lifecycle[--pending_lifecycle_count];
        }
        for (std::size_t i = 0; i < pending_fill_count;) {
            if (pending_fills[i].exchange_order_id.view() != order_id) {
                ++i;
                continue;
            }
            if (result.output_count >= output.size()) {
                result.output_overflow = 1;
                return;
            }
            const auto pending = pending_fills[i];
            route_fill(order_id, pending.trade_id.view(), pending.amount.view(),
                       pending.receive_ns, output, result);
            pending_fills[i] = pending_fills[--pending_fill_count];
        }
    }

    void clear_dedupe(std::string_view order_id) noexcept {
        for (auto& entry : dedupe) {
            if (entry.occupied && entry.exchange_order_id.view() == order_id) {
                entry.occupied = 0;
                entry.exchange_order_id.size = 0;
                entry.trade_id.size = 0;
            }
        }
    }

    void clear_pending(std::string_view order_id) noexcept {
        for (std::size_t i = 0; i < pending_fill_count;) {
            if (pending_fills[i].exchange_order_id.view() == order_id)
                pending_fills[i] = pending_fills[--pending_fill_count];
            else ++i;
        }
        for (std::size_t i = 0; i < pending_lifecycle_count;) {
            if (pending_lifecycle[i].exchange_order_id.view() == order_id)
                pending_lifecycle[i] = pending_lifecycle[--pending_lifecycle_count];
            else ++i;
        }
    }
};

UserOmsBridge::UserOmsBridge() : impl_(std::make_unique<Impl>()) {}
UserOmsBridge::~UserOmsBridge() = default;

UserOmsBridgeResult UserOmsBridge::on_post_order_ack(
    std::uint64_t client_order_id,
    std::string_view response_body,
    std::int64_t response_complete_monotonic_ns,
    std::span<RoutedOmsEvent> output) noexcept {

    UserOmsBridgeResult result;
    if (client_order_id == 0 || response_complete_monotonic_ns <= 0) {
        result.invalid_ack = 1;
        return result;
    }
    impl_->prune_pending(response_complete_monotonic_ns);
    const auto ack = clob_identity::decode_post_order_ack(response_body);
    if (!ack.valid) {
        result.invalid_ack = 1;
        return result;
    }
    if (!ack.success) {
        (void)impl_->emit(client_order_id, OmsEventType::Reject,
                          response_complete_monotonic_ns, 0, output, result);
        return result;
    }
    if (!impl_->identities.bind(ack.exchange_order_id.view(), client_order_id)) {
        result.identity_conflict = 1;
        return result;
    }
    (void)impl_->emit(client_order_id, OmsEventType::AckLive,
                      response_complete_monotonic_ns, 0, output, result);
    impl_->flush_pending(ack.exchange_order_id.view(), output, result);
    return result;
}

UserOmsBridgeResult UserOmsBridge::on_user_event(
    const user_ws::Event& event,
    std::span<RoutedOmsEvent> output) noexcept {

    UserOmsBridgeResult result;
    if (event.receive_monotonic_ns <= 0) return result;
    impl_->prune_pending(event.receive_monotonic_ns);

    if (event.kind == user_ws::EventKind::Order) {
        impl_->route_lifecycle(event.id.view(), event.order_type,
                               event.receive_monotonic_ns, output, result);
        return result;
    }
    if (event.kind != user_ws::EventKind::Trade
        || event.trade_status != user_ws::TradeStatus::Matched) return result;

    impl_->route_fill(event.taker_order_id.view(), event.id.view(),
                      event.trade_size.view(), event.receive_monotonic_ns,
                      output, result);
    for (std::size_t i = 0; i < event.maker_order_count; ++i) {
        const auto& maker = event.maker_orders[i];
        impl_->route_fill(maker.order_id.view(), event.id.view(),
                          maker.matched_amount.view(), event.receive_monotonic_ns,
                          output, result);
    }
    return result;
}

bool UserOmsBridge::release(std::uint64_t client_order_id) noexcept {
    const auto exchange = impl_->identities.lookup_exchange(client_order_id);
    if (!exchange.found) return false;
    // Copy because erase invalidates the view.
    clob_identity::FixedOrderId exact;
    if (!copy_id(exact, exchange.exchange_order_id)) return false;
    impl_->clear_pending(exact.view());
    impl_->clear_dedupe(exact.view());
    return impl_->identities.erase_client(client_order_id);
}

clob_identity::IdentityLookup UserOmsBridge::lookup_client(
    std::string_view exchange_order_id) const noexcept {
    return impl_->identities.lookup_client(exchange_order_id);
}

clob_identity::ExchangeLookup UserOmsBridge::lookup_exchange(
    std::uint64_t client_order_id) const noexcept {
    return impl_->identities.lookup_exchange(client_order_id);
}

UserOmsBridgeSnapshot UserOmsBridge::snapshot() const noexcept {
    return UserOmsBridgeSnapshot{
        impl_->identities.size(),
        impl_->pending_fill_count,
        impl_->pending_lifecycle_count,
        impl_->routed_fills,
        impl_->duplicate_fills,
        impl_->foreign_correlations,
        impl_->pending_overflow,
        impl_->dedupe_bucket_overflow,
    };
}

} // namespace pm::v7
