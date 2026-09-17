#pragma once

#include <cstdint>
#include <type_traits>

namespace pm::v7::clob_order {

// Single-owner nonzero uint64 salt sequence. One OS entropy read happens at
// cold startup; next() is syscall-free and allocation-free. Exchange V2 accepts
// uint256 salt, so every generated uint64 is represented exactly.
class OrderSaltSequence final {
public:
    OrderSaltSequence() noexcept = default;
    explicit OrderSaltSequence(std::uint64_t seed) noexcept;

    [[nodiscard]] static OrderSaltSequence from_os_entropy() noexcept;
    [[nodiscard]] bool valid() const noexcept { return valid_; }
    [[nodiscard]] std::uint64_t next() noexcept;

private:
    std::uint64_t seed_ = 0;
    std::uint64_t current_ = 0;
    bool valid_ = false;
    bool exhausted_ = false;
};

static_assert(std::is_trivially_copyable_v<OrderSaltSequence>);

} // namespace pm::v7::clob_order
