#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <span>
#include <string_view>

namespace pm::v7::clob_transport {

enum class Http1ResponseState : unsigned char {
    Receiving = 0,
    Complete = 1,
    Invalid = 2,
    UnsupportedFraming = 3,
    Overflow = 4,
};

class FixedHttp1Response final {
public:
    static constexpr std::size_t kCapacity = 64 * 1024;
    static constexpr std::size_t kMaxHeaderBytes = 16 * 1024;

    FixedHttp1Response() noexcept = default;

    void reset() noexcept;
    [[nodiscard]] std::span<char> writable() noexcept;
    [[nodiscard]] Http1ResponseState commit(std::size_t bytes_written) noexcept;

    [[nodiscard]] Http1ResponseState state() const noexcept { return state_; }
    [[nodiscard]] bool complete() const noexcept { return state_ == Http1ResponseState::Complete; }
    [[nodiscard]] int status_code() const noexcept { return status_code_; }
    [[nodiscard]] bool connection_close() const noexcept { return connection_close_; }
    [[nodiscard]] int retry_after_seconds() const noexcept { return retry_after_seconds_; }
    [[nodiscard]] double rate_limit_remaining() const noexcept { return rate_limit_remaining_; }
    [[nodiscard]] std::int64_t rate_limit_reset_unix_seconds() const noexcept {
        return rate_limit_reset_unix_seconds_;
    }
    [[nodiscard]] std::string_view rate_limit_tier() const noexcept {
        return {rate_limit_tier_.data(), rate_limit_tier_size_};
    }
    [[nodiscard]] bool rate_limit_warning() const noexcept { return rate_limit_warning_; }
    [[nodiscard]] std::size_t message_size() const noexcept { return message_size_; }
    [[nodiscard]] std::string_view body() const noexcept;

private:
    [[nodiscard]] Http1ResponseState parse_headers() noexcept;

    std::array<char, kCapacity> storage_{};
    std::size_t size_ = 0;
    std::size_t header_scan_from_ = 0;
    std::size_t header_end_ = 0;
    std::size_t content_length_ = 0;
    std::size_t message_size_ = 0;
    int status_code_ = 0;
    bool headers_parsed_ = false;
    bool connection_close_ = false;
    int retry_after_seconds_ = 0;
    double rate_limit_remaining_ = std::numeric_limits<double>::quiet_NaN();
    std::int64_t rate_limit_reset_unix_seconds_ = 0;
    std::array<char, 16> rate_limit_tier_{};
    std::uint8_t rate_limit_tier_size_ = 0;
    bool rate_limit_warning_ = false;
    Http1ResponseState state_ = Http1ResponseState::Receiving;
};

} // namespace pm::v7::clob_transport
