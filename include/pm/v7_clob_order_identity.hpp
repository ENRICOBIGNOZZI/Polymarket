#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <string_view>
#include <type_traits>

namespace pm::v7::clob_identity {

inline constexpr std::size_t kMaxExchangeOrderIdBytes = 96;
inline constexpr std::size_t kMaxAckErrorBytes = 256;
inline constexpr std::size_t kIdentityCapacity = 4096;

struct FixedOrderId {
    std::array<char, kMaxExchangeOrderIdBytes> bytes{};
    std::uint16_t size = 0;
    [[nodiscard]] std::string_view view() const noexcept {
        return {bytes.data(), size};
    }
};

struct PostOrderAck {
    FixedOrderId exchange_order_id{};
    std::array<char, kMaxAckErrorBytes> error_message{};
    std::uint16_t error_size = 0;
    std::uint8_t success = 0;
    std::uint8_t status_live = 0;
    std::uint8_t status_matched = 0;
    std::uint8_t status_delayed = 0;
    std::uint8_t status_unmatched = 0;
    std::uint8_t valid = 0;
    [[nodiscard]] std::string_view error() const noexcept {
        return {error_message.data(), error_size};
    }
};

[[nodiscard]] PostOrderAck decode_post_order_ack(std::string_view body) noexcept;

struct IdentityLookup {
    std::uint64_t client_order_id = 0;
    std::uint8_t found = 0;
};

struct ExchangeLookup {
    std::string_view exchange_order_id{};
    std::uint8_t found = 0;
};

// Single-owner, fixed-capacity exact identity map. Exchange IDs are never
// hashed into OMS handles: the hash is used only to locate the exact stored
// string, which is then compared byte-for-byte.
class OrderIdentityMap final {
public:
    OrderIdentityMap() noexcept = default;

    [[nodiscard]] bool bind(std::string_view exchange_order_id,
                            std::uint64_t client_order_id) noexcept;
    [[nodiscard]] IdentityLookup lookup_client(
        std::string_view exchange_order_id) const noexcept;
    [[nodiscard]] ExchangeLookup lookup_exchange(
        std::uint64_t client_order_id) const noexcept;
    [[nodiscard]] bool erase_exchange(std::string_view exchange_order_id) noexcept;
    [[nodiscard]] bool erase_client(std::uint64_t client_order_id) noexcept;
    [[nodiscard]] std::size_t size() const noexcept { return size_; }
    [[nodiscard]] bool full() const noexcept { return size_ >= kIdentityCapacity; }

private:
    enum class SlotState : std::uint8_t { Empty = 0, Occupied = 1, Tombstone = 2 };
    struct ExchangeSlot {
        FixedOrderId id{};
        std::uint64_t client_order_id = 0;
        std::uint32_t generation = 0;
        SlotState state = SlotState::Empty;
    };
    struct ClientSlot {
        std::uint64_t client_order_id = 0;
        std::uint32_t exchange_slot = 0;
        std::uint32_t exchange_generation = 0;
        SlotState state = SlotState::Empty;
    };

    [[nodiscard]] static std::uint64_t hash_exchange(std::string_view id) noexcept;
    [[nodiscard]] static std::uint64_t hash_client(std::uint64_t id) noexcept;
    [[nodiscard]] std::size_t find_exchange(std::string_view id) const noexcept;
    [[nodiscard]] std::size_t find_client(std::uint64_t id) const noexcept;
    [[nodiscard]] std::size_t find_exchange_insert(std::string_view id) const noexcept;
    [[nodiscard]] std::size_t find_client_insert(std::uint64_t id) const noexcept;
    [[nodiscard]] bool erase_indices(std::size_t exchange_slot,
                                     std::size_t client_slot) noexcept;

    std::array<ExchangeSlot, kIdentityCapacity> exchange_{};
    std::array<ClientSlot, kIdentityCapacity> client_{};
    std::size_t size_ = 0;
};

static_assert(std::is_trivially_copyable_v<PostOrderAck>);
static_assert(std::is_standard_layout_v<PostOrderAck>);

} // namespace pm::v7::clob_identity
