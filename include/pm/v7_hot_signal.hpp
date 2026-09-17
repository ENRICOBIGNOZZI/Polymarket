#pragma once

#include <atomic>
#include <cstdint>
#include <filesystem>
#include <string_view>

namespace pm::v7 {

class HotSignalDatagramSender final {
public:
    explicit HotSignalDatagramSender(const std::filesystem::path& destination);
    ~HotSignalDatagramSender();
    HotSignalDatagramSender(const HotSignalDatagramSender&) = delete;
    HotSignalDatagramSender& operator=(const HotSignalDatagramSender&) = delete;

    [[nodiscard]] bool send(std::string_view payload) noexcept;
    [[nodiscard]] std::uint64_t sent() const noexcept { return sent_.load(std::memory_order_relaxed); }
    [[nodiscard]] std::uint64_t unavailable() const noexcept { return unavailable_.load(std::memory_order_relaxed); }
    [[nodiscard]] std::uint64_t errors() const noexcept { return errors_.load(std::memory_order_relaxed); }
private:
    int fd_ = -1;
    std::filesystem::path destination_;
    std::atomic<std::uint64_t> sent_{0};
    std::atomic<std::uint64_t> unavailable_{0};
    std::atomic<std::uint64_t> errors_{0};
};

} // namespace pm::v7
