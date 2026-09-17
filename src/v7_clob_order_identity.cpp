#include "pm/v7_clob_order_identity.hpp"

#include <boost/json.hpp>
#include <boost/json/static_resource.hpp>

#include <algorithm>
#include <array>
#include <limits>

namespace pm::v7::clob_identity {
namespace {
namespace json = boost::json;
constexpr std::size_t kAckArenaBytes = 32U * 1024U;
constexpr std::size_t kMask = kIdentityCapacity - 1U;
static_assert((kIdentityCapacity & (kIdentityCapacity - 1U)) == 0U);

[[nodiscard]] const json::value* field(const json::object& object,
                                       std::string_view key) noexcept {
    const auto it = object.find(key);
    return it == object.end() ? nullptr : &it->value();
}

[[nodiscard]] const json::value* first_field(
    const json::object& object,
    std::initializer_list<std::string_view> keys) noexcept {
    for (const auto key : keys) {
        if (const auto* value = field(object, key); value != nullptr) return value;
    }
    return nullptr;
}

[[nodiscard]] std::string_view text(const json::value* value) noexcept {
    if (value == nullptr || !value->is_string()) return {};
    const auto& string = value->as_string();
    return {string.data(), string.size()};
}

[[nodiscard]] bool ieq(std::string_view lhs, std::string_view rhs) noexcept {
    if (lhs.size() != rhs.size()) return false;
    for (std::size_t i = 0; i < lhs.size(); ++i) {
        char a = lhs[i];
        char b = rhs[i];
        if (a >= 'A' && a <= 'Z') a = static_cast<char>(a - 'A' + 'a');
        if (b >= 'A' && b <= 'Z') b = static_cast<char>(b - 'A' + 'a');
        if (a != b) return false;
    }
    return true;
}

[[nodiscard]] bool copy_id(std::string_view source, FixedOrderId& output) noexcept {
    if (source.empty() || source.size() > output.bytes.size()
        || source.size() > std::numeric_limits<std::uint16_t>::max()) return false;
    std::copy(source.begin(), source.end(), output.bytes.begin());
    output.size = static_cast<std::uint16_t>(source.size());
    return true;
}

void copy_error(std::string_view source, PostOrderAck& output) noexcept {
    const auto size = std::min<std::size_t>(source.size(), output.error_message.size());
    std::copy_n(source.data(), size, output.error_message.data());
    output.error_size = static_cast<std::uint16_t>(size);
}

} // namespace

PostOrderAck decode_post_order_ack(std::string_view body) noexcept {
    PostOrderAck output;
    if (body.empty()) return output;
    try {
        thread_local std::array<unsigned char, kAckArenaBytes> arena{};
        json::static_resource resource(arena.data(), arena.size());
        boost::system::error_code error;
        const auto value = json::parse(body, error, &resource);
        if (error || !value.is_object()) return output;
        const auto& object = value.as_object();
        const auto* success = field(object, "success");
        if (success == nullptr || !success->is_bool()) return output;
        output.success = success->as_bool() ? 1 : 0;
        copy_error(text(first_field(object, {"errorMsg", "error"})), output);

        if (output.success == 0) {
            output.valid = 1;
            return output;
        }

        if (!copy_id(text(first_field(object, {"orderID", "orderId"})),
                     output.exchange_order_id)) {
            return PostOrderAck{};
        }
        const auto status = text(field(object, "status"));
        output.status_live = ieq(status, "live") ? 1 : 0;
        output.status_matched = ieq(status, "matched") ? 1 : 0;
        output.status_delayed = ieq(status, "delayed") ? 1 : 0;
        output.status_unmatched = ieq(status, "unmatched") ? 1 : 0;
        if (output.status_live == 0 && output.status_matched == 0
            && output.status_delayed == 0 && output.status_unmatched == 0) {
            return PostOrderAck{};
        }
        output.valid = 1;
        return output;
    } catch (...) {
        return PostOrderAck{};
    }
}

std::uint64_t OrderIdentityMap::hash_exchange(std::string_view id) noexcept {
    std::uint64_t hash = 14695981039346656037ULL;
    for (const unsigned char byte : id) {
        hash ^= byte;
        hash *= 1099511628211ULL;
    }
    return hash;
}

std::uint64_t OrderIdentityMap::hash_client(std::uint64_t id) noexcept {
    id ^= id >> 30U;
    id *= 0xbf58476d1ce4e5b9ULL;
    id ^= id >> 27U;
    id *= 0x94d049bb133111ebULL;
    id ^= id >> 31U;
    return id;
}

std::size_t OrderIdentityMap::find_exchange(std::string_view id) const noexcept {
    if (id.empty()) return kIdentityCapacity;
    std::size_t index = static_cast<std::size_t>(hash_exchange(id)) & kMask;
    for (std::size_t probe = 0; probe < kIdentityCapacity; ++probe) {
        const auto& slot = exchange_[index];
        if (slot.state == SlotState::Empty) return kIdentityCapacity;
        if (slot.state == SlotState::Occupied && slot.id.view() == id) return index;
        index = (index + 1U) & kMask;
    }
    return kIdentityCapacity;
}

std::size_t OrderIdentityMap::find_client(std::uint64_t id) const noexcept {
    if (id == 0) return kIdentityCapacity;
    std::size_t index = static_cast<std::size_t>(hash_client(id)) & kMask;
    for (std::size_t probe = 0; probe < kIdentityCapacity; ++probe) {
        const auto& slot = client_[index];
        if (slot.state == SlotState::Empty) return kIdentityCapacity;
        if (slot.state == SlotState::Occupied && slot.client_order_id == id) return index;
        index = (index + 1U) & kMask;
    }
    return kIdentityCapacity;
}

std::size_t OrderIdentityMap::find_exchange_insert(std::string_view id) const noexcept {
    std::size_t index = static_cast<std::size_t>(hash_exchange(id)) & kMask;
    std::size_t tombstone = kIdentityCapacity;
    for (std::size_t probe = 0; probe < kIdentityCapacity; ++probe) {
        const auto& slot = exchange_[index];
        if (slot.state == SlotState::Empty)
            return tombstone != kIdentityCapacity ? tombstone : index;
        if (slot.state == SlotState::Tombstone && tombstone == kIdentityCapacity)
            tombstone = index;
        index = (index + 1U) & kMask;
    }
    return tombstone;
}

std::size_t OrderIdentityMap::find_client_insert(std::uint64_t id) const noexcept {
    std::size_t index = static_cast<std::size_t>(hash_client(id)) & kMask;
    std::size_t tombstone = kIdentityCapacity;
    for (std::size_t probe = 0; probe < kIdentityCapacity; ++probe) {
        const auto& slot = client_[index];
        if (slot.state == SlotState::Empty)
            return tombstone != kIdentityCapacity ? tombstone : index;
        if (slot.state == SlotState::Tombstone && tombstone == kIdentityCapacity)
            tombstone = index;
        index = (index + 1U) & kMask;
    }
    return tombstone;
}

bool OrderIdentityMap::bind(std::string_view exchange_order_id,
                            std::uint64_t client_order_id) noexcept {
    if (exchange_order_id.empty() || exchange_order_id.size() > kMaxExchangeOrderIdBytes
        || client_order_id == 0) return false;

    const auto existing_exchange = find_exchange(exchange_order_id);
    if (existing_exchange != kIdentityCapacity)
        return exchange_[existing_exchange].client_order_id == client_order_id;
    if (find_client(client_order_id) != kIdentityCapacity || full()) return false;

    const auto exchange_index = find_exchange_insert(exchange_order_id);
    const auto client_index = find_client_insert(client_order_id);
    if (exchange_index == kIdentityCapacity || client_index == kIdentityCapacity) return false;

    auto& exchange_slot = exchange_[exchange_index];
    std::uint32_t generation = exchange_slot.generation + 1U;
    if (generation == 0) generation = 1;
    FixedOrderId exact;
    if (!copy_id(exchange_order_id, exact)) return false;
    exchange_slot.id = exact;
    exchange_slot.client_order_id = client_order_id;
    exchange_slot.generation = generation;
    exchange_slot.state = SlotState::Occupied;

    auto& client_slot = client_[client_index];
    client_slot.client_order_id = client_order_id;
    client_slot.exchange_slot = static_cast<std::uint32_t>(exchange_index);
    client_slot.exchange_generation = generation;
    client_slot.state = SlotState::Occupied;
    ++size_;
    return true;
}

IdentityLookup OrderIdentityMap::lookup_client(std::string_view exchange_order_id) const noexcept {
    const auto index = find_exchange(exchange_order_id);
    if (index == kIdentityCapacity) return {};
    return {exchange_[index].client_order_id, 1};
}

ExchangeLookup OrderIdentityMap::lookup_exchange(std::uint64_t client_order_id) const noexcept {
    const auto client_index = find_client(client_order_id);
    if (client_index == kIdentityCapacity) return {};
    const auto& client_slot = client_[client_index];
    if (client_slot.exchange_slot >= kIdentityCapacity) return {};
    const auto& exchange_slot = exchange_[client_slot.exchange_slot];
    if (exchange_slot.state != SlotState::Occupied
        || exchange_slot.generation != client_slot.exchange_generation
        || exchange_slot.client_order_id != client_order_id) return {};
    return {exchange_slot.id.view(), 1};
}

bool OrderIdentityMap::erase_indices(std::size_t exchange_slot,
                                     std::size_t client_slot) noexcept {
    if (exchange_slot >= kIdentityCapacity || client_slot >= kIdentityCapacity) return false;
    exchange_[exchange_slot].id.size = 0;
    exchange_[exchange_slot].client_order_id = 0;
    exchange_[exchange_slot].state = SlotState::Tombstone;
    client_[client_slot].client_order_id = 0;
    client_[client_slot].exchange_slot = 0;
    client_[client_slot].exchange_generation = 0;
    client_[client_slot].state = SlotState::Tombstone;
    if (size_ > 0) --size_;
    return true;
}

bool OrderIdentityMap::erase_exchange(std::string_view exchange_order_id) noexcept {
    const auto exchange_index = find_exchange(exchange_order_id);
    if (exchange_index == kIdentityCapacity) return false;
    const auto client_index = find_client(exchange_[exchange_index].client_order_id);
    if (client_index == kIdentityCapacity) return false;
    return erase_indices(exchange_index, client_index);
}

bool OrderIdentityMap::erase_client(std::uint64_t client_order_id) noexcept {
    const auto client_index = find_client(client_order_id);
    if (client_index == kIdentityCapacity) return false;
    const auto exchange_index = client_[client_index].exchange_slot;
    if (exchange_index >= kIdentityCapacity) return false;
    const auto& exchange_slot = exchange_[exchange_index];
    if (exchange_slot.state != SlotState::Occupied
        || exchange_slot.generation != client_[client_index].exchange_generation
        || exchange_slot.client_order_id != client_order_id) return false;
    return erase_indices(exchange_index, client_index);
}

} // namespace pm::v7::clob_identity
