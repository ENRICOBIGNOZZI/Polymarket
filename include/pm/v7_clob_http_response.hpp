#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <span>
#include <string_view>

namespace pm::v7::clob_http_response {

inline constexpr std::size_t kMaxResponseHeaderBytes = 4096;
inline constexpr std::size_t kMaxResponseBodyBytes = 16384;

enum class Framing : std::uint8_t {
    Unknown = 0,
    ContentLength = 1,
    Chunked = 2,
    NoBody = 3,
};

enum class ParseError : std::uint8_t {
    None = 0,
    InvalidTimestamp,
    HeaderTooLarge,
    InvalidStatusLine,
    InvalidHeader,
    InvalidContentLength,
    ConflictingFraming,
    UnsupportedFraming,
    BodyTooLarge,
    InvalidChunkSize,
    InvalidChunkTerminator,
    InvalidTrailer,
    DataAfterComplete,
};

struct ResponseSnapshot {
    int status_code = 0;
    Framing framing = Framing::Unknown;
    ParseError error = ParseError::None;
    std::size_t body_size = 0;
    std::int64_t first_byte_monotonic_ns = 0;
    std::int64_t complete_monotonic_ns = 0;
    std::int64_t first_byte_to_complete_ns = 0;
    std::uint8_t headers_complete = 0;
    std::uint8_t complete = 0;
    std::uint8_t http_success = 0;
    std::uint8_t body_captured = 0;
};

// Single-owner reusable parser for a persistent HTTP/1.1 execution lane.
// It performs no allocation and records local monotonic first-byte and
// completion timestamps supplied by the caller's SSL_read boundary.
class ResponseParser final {
public:
    ResponseParser() noexcept = default;
    void reset() noexcept;

    [[nodiscard]] bool consume(
        std::span<const char> bytes,
        std::int64_t receive_monotonic_ns) noexcept;

    [[nodiscard]] ResponseSnapshot snapshot() const noexcept;
    [[nodiscard]] std::string_view body() const noexcept {
        return std::string_view(body_.data(), body_size_);
    }

private:
    enum class ChunkState : std::uint8_t {
        SizeLine = 0,
        Data = 1,
        DataCr = 2,
        DataLf = 3,
        Trailers = 4,
    };

    [[nodiscard]] bool fail(ParseError error) noexcept;
    [[nodiscard]] bool process_header() noexcept;
    [[nodiscard]] bool consume_body_byte(char c) noexcept;
    [[nodiscard]] bool append_body(char c) noexcept;
    [[nodiscard]] bool finish(std::int64_t receive_monotonic_ns) noexcept;

    std::array<char, kMaxResponseHeaderBytes> header_{};
    std::array<char, kMaxResponseBodyBytes> body_{};
    std::size_t header_size_ = 0;
    std::size_t body_size_ = 0;
    std::size_t content_length_ = 0;
    std::size_t chunk_remaining_ = 0;
    std::size_t chunk_value_ = 0;
    std::size_t trailer_line_size_ = 0;
    std::uint8_t chunk_digits_ = 0;
    std::uint8_t chunk_extension_ = 0;
    std::uint8_t chunk_line_cr_ = 0;
    std::uint8_t trailer_cr_ = 0;
    std::uint8_t header_match_ = 0;
    int status_code_ = 0;
    Framing framing_ = Framing::Unknown;
    ParseError error_ = ParseError::None;
    ChunkState chunk_state_ = ChunkState::SizeLine;
    std::int64_t first_byte_ns_ = 0;
    std::int64_t last_receive_ns_ = 0;
    std::int64_t complete_ns_ = 0;
    bool headers_complete_ = false;
    bool complete_ = false;
};

} // namespace pm::v7::clob_http_response
