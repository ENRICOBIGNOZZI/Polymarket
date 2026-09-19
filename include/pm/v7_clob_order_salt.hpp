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
    explicit OrderSaltSequence(std::uint64_t seed) noexcept
        : seed_(seed), current_(seed), valid_(seed != 0) {}

    [[nodiscard]] static OrderSaltSequence from_os_entropy() noexcept;
    [[nodiscard]] bool valid() const noexcept { return valid_; }

    // This executes once per signed order. Keep it visible to the caller so an
    // ordinary Release build can inline the increment without requiring LTO.
    [[nodiscard]] std::uint64_t next() noexcept {
        if (!valid_ || exhausted_) return 0;
        const std::uint64_t out = current_;
        ++current_; // unsigned wrap is defined.
        if (current_ == 0) [[unlikely]] current_ = 1; // zero is never a salt.
        if (current_ == seed_) [[unlikely]] exhausted_ = true;
        return out;
    }

private:
    std::uint64_t seed_ = 0;
    std::uint64_t current_ = 0;
    bool valid_ = false;
    bool exhausted_ = false;
};

static_assert(std::is_trivially_copyable_v<OrderSaltSequence>);

} // namespace pm::v7::clob_order
