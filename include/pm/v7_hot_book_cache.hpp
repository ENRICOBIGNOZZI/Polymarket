#pragma once

#include "pm/v7_market_state.hpp"

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <string_view>
#include <vector>

namespace pm::v7 {

inline constexpr std::size_t kHotBookCacheHeaderBytes = 64;
inline constexpr std::size_t kHotBookCacheSlotBytes = 512;
inline constexpr std::size_t kHotBookCacheMaxSlots = 128;
inline constexpr std::string_view kHotBookCacheMagic = "V7HBK001";
inline constexpr std::uint32_t kHotBookCacheVersion = 1;

class HotBookCacheWriter final {
public:
    HotBookCacheWriter(const std::filesystem::path& path, std::string_view model_sha,
                       std::size_t max_instrument_handle);
    ~HotBookCacheWriter();
    HotBookCacheWriter(const HotBookCacheWriter&) = delete;
    HotBookCacheWriter& operator=(const HotBookCacheWriter&) = delete;

    [[nodiscard]] bool publish(std::uint64_t instrument_handle,
                               std::string_view market_id,
                               std::string_view token_id,
                               const BookHotSnapshot& book,
                               std::int64_t receive_wall_ms) noexcept;
    [[nodiscard]] std::uint64_t publications() const noexcept { return publications_.load(std::memory_order_relaxed); }
    [[nodiscard]] std::uint64_t failures() const noexcept { return failures_.load(std::memory_order_relaxed); }

private:
    int fd_ = -1;
    std::byte* mapping_ = nullptr;
    std::size_t bytes_ = 0;
    std::size_t max_handle_ = 0;
    std::vector<std::uint64_t> sequences_{};
    std::atomic<std::uint64_t> publications_{0};
    std::atomic<std::uint64_t> failures_{0};
};

} // namespace pm::v7
