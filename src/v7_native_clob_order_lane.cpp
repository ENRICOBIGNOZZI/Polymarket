#include "pm/v7_native_clob_order_lane.hpp"

#include "pm/v7_clob_http_frame.hpp"
#include "pm/v7_clob_http1_response.hpp"
#include "pm/v7_clob_order_amounts.hpp"
#include "pm/v7_clob_order_salt.hpp"
#include "pm/v7_clob_prepared_post.hpp"
#include "pm/v7_clob_transport_pool.hpp"
#include "pm/v7_clob_wire.hpp"
#include "pm/v7_poly1271.hpp"

#include <array>
#include <charconv>
#include <chrono>
#include <cstring>
#include <limits>
#include <string_view>

namespace pm::v7 {
namespace {

[[nodiscard]] std::int64_t now_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

template <std::size_t N>
struct FixedText final {
    std::array<char, N> bytes{};
    std::size_t size = 0;
    [[nodiscard]] bool assign(std::string_view value) noexcept {
        if (value.empty() || value.size() >= N) return false;
        std::memcpy(bytes.data(), value.data(), value.size());
        size = value.size();
        return true;
    }
    [[nodiscard]] std::string_view view() const noexcept {
        return {bytes.data(), size};
    }
};

template <typename T, std::size_t N>
[[nodiscard]] std::string_view decimal(T value, std::array<char, N>& buffer) noexcept {
    const auto result = std::to_chars(buffer.data(), buffer.data() + buffer.size(), value);
    if (result.ec != std::errc{}) return {};
    return {buffer.data(), static_cast<std::size_t>(result.ptr - buffer.data())};
}

[[nodiscard]] bool same_hex_address(std::string_view lhs, std::string_view rhs) noexcept {
    if (lhs.size() != rhs.size() || lhs.size() != 42) return false;
    for (std::size_t i = 0; i < lhs.size(); ++i) {
        char a = lhs[i], b = rhs[i];
        if (a >= 'A' && a <= 'F') a = static_cast<char>(a - 'A' + 'a');
        if (b >= 'A' && b <= 'F') b = static_cast<char>(b - 'A' + 'a');
        if (a != b) return false;
    }
    return true;
}
[[nodiscard]] NativeClobSubmitResult fail_before_wire(
    NativeOrderTxOwner& owner, std::uint64_t client_order_id,
    NativeClobSubmitReason reason) noexcept {
    NativeClobSubmitResult out;
    out.reason = reason;
    out.client_order_id = client_order_id;
    OmsEvent event{};
    event.type = OmsEventType::Reject;
    event.timestamp_ns = now_ns();
    const auto transition = owner.apply_owned(client_order_id, event);
    out.final_state = transition.state;
    return out;
}

[[nodiscard]] NativeClobSubmitResult fail_after_wire(
    NativeOrderTxOwner& owner, std::uint64_t client_order_id,
    NativeClobSubmitReason reason, std::int64_t wire_ns = 0) noexcept {
    NativeClobSubmitResult out;
    out.reason = reason;
    out.client_order_id = client_order_id;
    out.wire_monotonic_ns = wire_ns;
    OmsEvent event{};
    event.type = OmsEventType::TransportUnknown;
    event.timestamp_ns = now_ns();
    const auto transition = owner.apply_owned(client_order_id, event);
    out.final_state = transition.state;
    return out;
}

} // namespace

struct NativeClobOrderLane::Impl final {
    FixedText<128> deposit_wallet{};
    FixedText<128> signer_eoa{};
    FixedText<128> token_id{};
    FixedText<128> metadata{};
    FixedText<128> builder{};
    FixedText<128> api_key{};
    FixedText<128> passphrase{};
    FixedText<128> poly_address{};

    poly1271::Poly1271OrderHasher order_hasher;
    poly1271::Secp256k1Signer signer;
    clob_order::OrderSaltSequence salt;
    clob_post::PreparedPostOrderBuilder buy_fak;
    clob_post::PreparedPostOrderBuilder sell_fak;
    clob_post::PreparedPostOrderBuilder buy_fok;
    clob_post::PreparedPostOrderBuilder sell_fok;
    clob::DualPersistentTlsTransport transport;
    clob_transport::FixedHttp1Response response_parser{};
    bool valid = false;

    Impl(const NativeClobLaneConfig& config,
         std::span<const std::uint8_t, 32> private_key) noexcept
        : order_hasher({config.chain_id, config.exchange_contract}, config.deposit_wallet),
          signer(private_key),
          salt(clob_order::OrderSaltSequence::from_os_entropy()),
          buy_fak({config.builder_hex, "0", config.deposit_wallet, config.metadata_hex,
                   "BUY", 3, config.deposit_wallet, config.token_id_decimal, config.api_key,
                   clob_wire::MarketOrderType::FAK},
                  config.signer_eoa_address, config.api_key, config.passphrase, config.l2_secret_base64),
          sell_fak({config.builder_hex, "0", config.deposit_wallet, config.metadata_hex,
                    "SELL", 3, config.deposit_wallet, config.token_id_decimal, config.api_key,
                    clob_wire::MarketOrderType::FAK},
                   config.signer_eoa_address, config.api_key, config.passphrase, config.l2_secret_base64),
          buy_fok({config.builder_hex, "0", config.deposit_wallet, config.metadata_hex,
                   "BUY", 3, config.deposit_wallet, config.token_id_decimal, config.api_key,
                   clob_wire::MarketOrderType::FOK},
                  config.signer_eoa_address, config.api_key, config.passphrase, config.l2_secret_base64),
          sell_fok({config.builder_hex, "0", config.deposit_wallet, config.metadata_hex,
                    "SELL", 3, config.deposit_wallet, config.token_id_decimal, config.api_key,
                    clob_wire::MarketOrderType::FOK},
                   config.signer_eoa_address, config.api_key, config.passphrase, config.l2_secret_base64),
          transport(config.host, config.port, config.timeout_ms) {
        std::array<char, 42> derived{};
        if (!deposit_wallet.assign(config.deposit_wallet)
            || !signer_eoa.assign(config.signer_eoa_address)
            || !token_id.assign(config.token_id_decimal)
            || !metadata.assign(config.metadata_hex)
            || !builder.assign(config.builder_hex)
            || !api_key.assign(config.api_key)
            || !passphrase.assign(config.passphrase)
            || !poly_address.assign(config.signer_eoa_address)
            || !order_hasher.valid() || !signer.valid() || !salt.valid()
            || !buy_fak.valid() || !sell_fak.valid() || !buy_fok.valid() || !sell_fok.valid()
            || !signer.address_hex(derived)
            || !same_hex_address({derived.data(), derived.size()}, signer_eoa.view())) {
            return;
        }
        valid = true;
    }
};

NativeClobOrderLane::NativeClobOrderLane(
    const NativeClobLaneConfig& config,
    std::span<const std::uint8_t, 32> private_key) noexcept
    : impl_(std::make_unique<Impl>(config, private_key)) {}

NativeClobOrderLane::~NativeClobOrderLane() = default;

bool NativeClobOrderLane::valid() const noexcept {
    return impl_ != nullptr && impl_->valid;
}

bool NativeClobOrderLane::connect(std::string_view ca_file) noexcept {
    if (!valid()) return false;
    return impl_->transport.connect(ca_file).ready != 0;
}
void NativeClobOrderLane::close() noexcept {
    if (impl_) impl_->transport.close();
}

bool NativeClobOrderLane::connected() const noexcept {
    return valid() && impl_->transport.ready();
}

NativeClobSubmitResult NativeClobOrderLane::submit(
    NativeOrderTxOwner& oms_owner,
    UserOmsBridge& account_bridge,
    const NativeOrderCommand& command,
    std::uint64_t wall_timestamp_ms,
    std::span<RoutedOmsEvent> routed_scratch) noexcept {
    NativeClobSubmitResult out;
    out.client_order_id = command.client_order_id;
    if (!valid()) {
        out.reason = NativeClobSubmitReason::InvalidConfiguration;
        return out;
    }
    const auto* record = oms_owner.find(command.client_order_id);
    if (record == nullptr || record->state != OrderState::SendPending
        || command.client_order_id == 0 || command.quantity_microunits <= 0
        || command.price_tick <= 0 || command.tick_size_e4 <= 0
        || wall_timestamp_ms == 0 || routed_scratch.size() < 4
        || (command.side != Side::Buy && command.side != Side::Sell)
        || (command.time_in_force != AdapterTimeInForce::Fak
            && command.time_in_force != AdapterTimeInForce::Fok)) {
        return fail_before_wire(oms_owner, command.client_order_id,
                                NativeClobSubmitReason::InvalidCommand);
    }
    const std::int64_t price_e4_wide =
        command.price_tick * static_cast<std::int64_t>(command.tick_size_e4);
    if (price_e4_wide <= 0 || price_e4_wide >= 10'000
        || price_e4_wide > std::numeric_limits<std::int32_t>::max()) {
        return fail_before_wire(oms_owner, command.client_order_id,
                                NativeClobSubmitReason::InvalidCommand);
    }
    const auto price_e4 = static_cast<std::int32_t>(price_e4_wide);
    const auto amounts = clob_order::marketable_limit_amounts(
        command.side, price_e4, command.tick_size_e4, command.quantity_microunits);
    if (!amounts.valid) {
        return fail_before_wire(oms_owner, command.client_order_id,
                                NativeClobSubmitReason::PreWireFailure);
    }

    const auto salt = impl_->salt.next();
    if (salt == 0) {
        return fail_before_wire(oms_owner, command.client_order_id,
                                NativeClobSubmitReason::PreWireFailure);
    }

    std::array<char, 32> salt_text{}, maker_text{}, taker_text{}, timestamp_text{};
    std::array<char, 24> request_timestamp_text{};
    const auto salt_sv = decimal(salt, salt_text);
    const auto maker_sv = decimal(amounts.maker_amount, maker_text);
    const auto taker_sv = decimal(amounts.taker_amount, taker_text);
    const auto timestamp_sv = decimal(wall_timestamp_ms, timestamp_text);
    const auto request_ts_sv = decimal(wall_timestamp_ms / 1000U, request_timestamp_text);
    if (salt_sv.empty() || maker_sv.empty() || taker_sv.empty()
        || timestamp_sv.empty() || request_ts_sv.empty()) {
        return fail_before_wire(oms_owner, command.client_order_id,
                                NativeClobSubmitReason::PreWireFailure);
    }
    clob_eip712::ExchangeV2OrderView order{};
    order.salt_decimal = salt_sv;
    order.maker = impl_->deposit_wallet.view();
    order.signer = impl_->deposit_wallet.view();
    order.token_id_decimal = impl_->token_id.view();
    order.maker_amount_decimal = maker_sv;
    order.taker_amount_decimal = taker_sv;
    order.side = command.side == Side::Buy ? 0 : 1;
    order.signature_type = 3;
    order.timestamp_decimal = timestamp_sv;
    order.metadata_hex = impl_->metadata.view();
    order.builder_hex = impl_->builder.view();

    std::array<char, poly1271::kWrappedSignatureHexChars> order_signature{};
    if (!poly1271::sign_poly1271_hex(
            impl_->order_hasher, impl_->signer, order, order_signature)) {
        return fail_before_wire(oms_owner, command.client_order_id,
                                NativeClobSubmitReason::PreWireFailure);
    }

    clob_wire::MarketOrderDynamicView dynamic{};
    dynamic.maker_amount = maker_sv;
    dynamic.salt_decimal = salt_sv;
    dynamic.signature = {order_signature.data(), order_signature.size()};
    dynamic.taker_amount = taker_sv;
    dynamic.timestamp_ms = timestamp_sv;

    clob_post::PreparedPostOrderBuilder* post = nullptr;
    if (command.side == Side::Buy && command.time_in_force == AdapterTimeInForce::Fak)
        post = &impl_->buy_fak;
    else if (command.side == Side::Sell && command.time_in_force == AdapterTimeInForce::Fak)
        post = &impl_->sell_fak;
    else if (command.side == Side::Buy && command.time_in_force == AdapterTimeInForce::Fok)
        post = &impl_->buy_fok;
    else if (command.side == Side::Sell && command.time_in_force == AdapterTimeInForce::Fok)
        post = &impl_->sell_fok;
    if (post == nullptr) {
        return fail_before_wire(oms_owner, command.client_order_id,
                                NativeClobSubmitReason::PreWireFailure);
    }

    std::array<char, 8192> frame{};
    const auto frame_size = post->build(dynamic, request_ts_sv, frame);
    if (frame_size == 0) {
        return fail_before_wire(oms_owner, command.client_order_id,
                                NativeClobSubmitReason::PreWireFailure);
    }
    auto& tls = impl_->transport.lane(clob::TransportLane::Order);
    if (!tls.connected()) {
        return fail_before_wire(oms_owner, command.client_order_id,
                                NativeClobSubmitReason::TransportFailure);
    }
    const auto write = tls.write_all({frame.data(), frame_size});
    if (!write.ok) {
        return fail_after_wire(oms_owner, command.client_order_id,
                               NativeClobSubmitReason::TransportFailure);
    }
    out.wire_monotonic_ns = write.completed_monotonic_ns;
    OmsEvent wire_event{};
    wire_event.type = OmsEventType::WireSend;
    wire_event.timestamp_ns = write.completed_monotonic_ns;
    const auto wire_transition = oms_owner.apply_owned(command.client_order_id, wire_event);
    if (!wire_transition.applied || wire_transition.state != OrderState::AckPending) {
        return fail_after_wire(oms_owner, command.client_order_id,
                               NativeClobSubmitReason::OmsFailure,
                               write.completed_monotonic_ns);
    }

    impl_->response_parser.reset();
    std::int64_t response_complete_ns = 0;
    while (!impl_->response_parser.complete()) {
        auto writable = impl_->response_parser.writable();
        if (writable.empty()) {
            return fail_after_wire(oms_owner, command.client_order_id,
                                   NativeClobSubmitReason::ResponseFailure,
                                   write.completed_monotonic_ns);
        }
        const auto read = tls.read_some(writable);
        if (!read.ok || read.bytes == 0) {
            return fail_after_wire(oms_owner, command.client_order_id,
                                   NativeClobSubmitReason::ResponseFailure,
                                   write.completed_monotonic_ns);
        }
        const auto state = impl_->response_parser.commit(read.bytes);
        if (state == clob_transport::Http1ResponseState::Complete) {
            response_complete_ns = read.completed_monotonic_ns;
            break;
        }
        if (state != clob_transport::Http1ResponseState::Receiving) {
            return fail_after_wire(oms_owner, command.client_order_id,
                                   NativeClobSubmitReason::ResponseFailure,
                                   write.completed_monotonic_ns);
        }
    }
    if (response_complete_ns <= 0) {
        return fail_after_wire(oms_owner, command.client_order_id,
                               NativeClobSubmitReason::ResponseFailure,
                               write.completed_monotonic_ns);
    }
    out.http_status = impl_->response_parser.status_code();
    out.response_complete_monotonic_ns = response_complete_ns;
    const auto bridge_result = account_bridge.on_post_order_ack(
        command.client_order_id, impl_->response_parser.body(),
        response_complete_ns, routed_scratch);
    if (bridge_result.invalid_ack || bridge_result.identity_conflict
        || bridge_result.output_overflow || bridge_result.pending_overflow
        || bridge_result.output_count == 0) {
        return fail_after_wire(oms_owner, command.client_order_id,
                               NativeClobSubmitReason::AckFailure,
                               write.completed_monotonic_ns);
    }

    for (std::size_t i = 0; i < bridge_result.output_count; ++i) {
        const auto& routed = routed_scratch[i];
        if (routed.client_order_id != command.client_order_id) {
            return fail_after_wire(oms_owner, command.client_order_id,
                                   NativeClobSubmitReason::OmsFailure,
                                   write.completed_monotonic_ns);
        }
        const auto transition = oms_owner.apply_owned(
            routed.client_order_id, routed.event);
        if (transition.invariant_violation || transition.reconciliation_required) {
            return fail_after_wire(oms_owner, command.client_order_id,
                                   NativeClobSubmitReason::OmsFailure,
                                   write.completed_monotonic_ns);
        }
    }

    const auto* final_record = oms_owner.find(command.client_order_id);
    if (final_record == nullptr) {
        return fail_after_wire(oms_owner, command.client_order_id,
                               NativeClobSubmitReason::OmsFailure,
                               write.completed_monotonic_ns);
    }
    out.final_state = final_record->state;
    out.identity_bound = account_bridge.lookup_exchange(command.client_order_id).found;
    if (final_record->state == OrderState::Rejected) {
        out.reason = NativeClobSubmitReason::AckFailure;
        return out;
    }
    if (!out.identity_bound
        || (final_record->state != OrderState::Live
            && final_record->state != OrderState::Partial
            && final_record->state != OrderState::Filled)) {
        return fail_after_wire(oms_owner, command.client_order_id,
                               NativeClobSubmitReason::OmsFailure,
                               write.completed_monotonic_ns);
    }
    out.reason = NativeClobSubmitReason::Accepted;
    out.accepted = 1;
    return out;
}

} // namespace pm::v7
