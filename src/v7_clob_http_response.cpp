#include "pm/v7_clob_http_response.hpp"

#include <charconv>
#include <limits>

namespace pm::v7::clob_http_response {
namespace {

[[nodiscard]] unsigned char ascii_lower(unsigned char c) noexcept {
    return c >= 'A' && c <= 'Z'
        ? static_cast<unsigned char>(c + ('a' - 'A')) : c;
}

[[nodiscard]] bool iequals(std::string_view a, std::string_view b) noexcept {
    if (a.size() != b.size()) return false;
    for (std::size_t i = 0; i < a.size(); ++i) {
        if (ascii_lower(static_cast<unsigned char>(a[i]))
            != ascii_lower(static_cast<unsigned char>(b[i]))) return false;
    }
    return true;
}

[[nodiscard]] std::string_view trim(std::string_view value) noexcept {
    while (!value.empty() && (value.front() == ' ' || value.front() == '\t'))
        value.remove_prefix(1);
    while (!value.empty() && (value.back() == ' ' || value.back() == '\t'))
        value.remove_suffix(1);
    return value;
}

[[nodiscard]] bool token_contains(
    std::string_view value,
    std::string_view token) noexcept {
    while (!value.empty()) {
        const auto comma = value.find(',');
        const auto part = trim(value.substr(0, comma));
        if (iequals(part, token)) return true;
        if (comma == std::string_view::npos) return false;
        value.remove_prefix(comma + 1);
    }
    return false;
}

[[nodiscard]] int hex_digit(unsigned char c) noexcept {
    if (c >= '0' && c <= '9') return static_cast<int>(c - '0');
    if (c >= 'a' && c <= 'f') return static_cast<int>(c - 'a') + 10;
    if (c >= 'A' && c <= 'F') return static_cast<int>(c - 'A') + 10;
    return -1;
}

} // namespace

void ResponseParser::reset() noexcept {
    header_size_ = 0;
    body_size_ = 0;
    content_length_ = 0;
    chunk_remaining_ = 0;
    chunk_value_ = 0;
    trailer_line_size_ = 0;
    chunk_digits_ = 0;
    chunk_extension_ = 0;
    chunk_line_cr_ = 0;
    trailer_cr_ = 0;
    header_match_ = 0;
    status_code_ = 0;
    framing_ = Framing::Unknown;
    error_ = ParseError::None;
    chunk_state_ = ChunkState::SizeLine;
    first_byte_ns_ = 0;
    last_receive_ns_ = 0;
    complete_ns_ = 0;
    headers_complete_ = false;
    complete_ = false;
}

bool ResponseParser::fail(ParseError error) noexcept {
    if (error_ == ParseError::None) error_ = error;
    return false;
}

bool ResponseParser::append_body(char c) noexcept {
    if (body_size_ >= body_.size()) return fail(ParseError::BodyTooLarge);
    body_[body_size_++] = c;
    return true;
}

bool ResponseParser::process_header() noexcept {
    const std::string_view header(header_.data(), header_size_);
    const auto first_eol = header.find("\r\n");
    if (first_eol == std::string_view::npos)
        return fail(ParseError::InvalidStatusLine);
    const auto status_line = header.substr(0, first_eol);
    if (!status_line.starts_with("HTTP/1.1 ")
        && !status_line.starts_with("HTTP/1.0 ")) {
        return fail(ParseError::InvalidStatusLine);
    }
    if (status_line.size() < 12) return fail(ParseError::InvalidStatusLine);
    const auto status_text = status_line.substr(9, 3);
    int code = 0;
    const auto [end, ec] = std::from_chars(
        status_text.data(), status_text.data() + status_text.size(), code);
    if (ec != std::errc{}
        || end != status_text.data() + status_text.size()
        || code < 100 || code > 599) {
        return fail(ParseError::InvalidStatusLine);
    }
    status_code_ = code;

    bool seen_content_length = false;
    bool seen_transfer_encoding = false;
    std::size_t parsed_content_length = 0;
    std::size_t cursor = first_eol + 2;
    while (cursor < header.size()) {
        const auto eol = header.find("\r\n", cursor);
        if (eol == std::string_view::npos) return fail(ParseError::InvalidHeader);
        if (eol == cursor) break;
        const auto line = header.substr(cursor, eol - cursor);
        const auto colon = line.find(':');
        if (colon == std::string_view::npos || colon == 0)
            return fail(ParseError::InvalidHeader);
        const auto name = line.substr(0, colon);
        const auto value = trim(line.substr(colon + 1));
        for (const unsigned char c : name) {
            if (c <= 0x20U || c >= 0x7fU)
                return fail(ParseError::InvalidHeader);
        }
        if (iequals(name, "Content-Length")) {
            if (value.empty()) return fail(ParseError::InvalidContentLength);
            std::size_t parsed = 0;
            const auto [p, length_ec] = std::from_chars(
                value.data(), value.data() + value.size(), parsed);
            if (length_ec != std::errc{}
                || p != value.data() + value.size()) {
                return fail(ParseError::InvalidContentLength);
            }
            if (seen_content_length && parsed != parsed_content_length)
                return fail(ParseError::InvalidContentLength);
            seen_content_length = true;
            parsed_content_length = parsed;
        } else if (iequals(name, "Transfer-Encoding")) {
            if (!token_contains(value, "chunked"))
                return fail(ParseError::UnsupportedFraming);
            seen_transfer_encoding = true;
        }
        cursor = eol + 2;
    }

    const bool status_has_no_body =
        (status_code_ >= 100 && status_code_ < 200)
        || status_code_ == 204 || status_code_ == 304;
    if (status_has_no_body) {
        if ((seen_content_length && parsed_content_length != 0)
            || seen_transfer_encoding) {
            return fail(ParseError::ConflictingFraming);
        }
        framing_ = Framing::NoBody;
        headers_complete_ = true;
        return true;
    }
    if (seen_content_length && seen_transfer_encoding)
        return fail(ParseError::ConflictingFraming);
    if (seen_transfer_encoding) {
        framing_ = Framing::Chunked;
    } else if (seen_content_length) {
        if (parsed_content_length > body_.size())
            return fail(ParseError::BodyTooLarge);
        framing_ = Framing::ContentLength;
        content_length_ = parsed_content_length;
    } else {
        return fail(ParseError::UnsupportedFraming);
    }
    headers_complete_ = true;
    return true;
}

bool ResponseParser::consume_body_byte(char c) noexcept {
    if (framing_ == Framing::ContentLength) return append_body(c);
    if (framing_ != Framing::Chunked)
        return fail(ParseError::UnsupportedFraming);

    switch (chunk_state_) {
        case ChunkState::SizeLine: {
            if (chunk_line_cr_ != 0) {
                if (c != '\n') return fail(ParseError::InvalidChunkSize);
                chunk_remaining_ = chunk_value_;
                chunk_value_ = 0;
                chunk_digits_ = 0;
                chunk_extension_ = 0;
                chunk_line_cr_ = 0;
                if (chunk_remaining_ == 0) {
                    chunk_state_ = ChunkState::Trailers;
                    trailer_line_size_ = 0;
                    trailer_cr_ = 0;
                } else {
                    if (chunk_remaining_ > body_.size() - body_size_)
                        return fail(ParseError::BodyTooLarge);
                    chunk_state_ = ChunkState::Data;
                }
                return true;
            }
            if (c == '\r') {
                if (chunk_digits_ == 0) return fail(ParseError::InvalidChunkSize);
                chunk_line_cr_ = 1;
                return true;
            }
            if (c == '\n') return fail(ParseError::InvalidChunkSize);
            if (chunk_extension_ != 0) {
                const auto uc = static_cast<unsigned char>(c);
                if (uc < 0x20U || uc > 0x7eU)
                    return fail(ParseError::InvalidChunkSize);
                return true;
            }
            if (c == ';') {
                if (chunk_digits_ == 0) return fail(ParseError::InvalidChunkSize);
                chunk_extension_ = 1;
                return true;
            }
            const int digit = hex_digit(static_cast<unsigned char>(c));
            if (digit < 0) return fail(ParseError::InvalidChunkSize);
            if (chunk_value_
                > (std::numeric_limits<std::size_t>::max()
                   - static_cast<std::size_t>(digit)) / 16U) {
                return fail(ParseError::InvalidChunkSize);
            }
            chunk_value_ = chunk_value_ * 16U + static_cast<std::size_t>(digit);
            if (chunk_digits_ == std::numeric_limits<std::uint8_t>::max())
                return fail(ParseError::InvalidChunkSize);
            ++chunk_digits_;
            return true;
        }
        case ChunkState::Data:
            if (!append_body(c)) return false;
            if (--chunk_remaining_ == 0) chunk_state_ = ChunkState::DataCr;
            return true;
        case ChunkState::DataCr:
            if (c != '\r') return fail(ParseError::InvalidChunkTerminator);
            chunk_state_ = ChunkState::DataLf;
            return true;
        case ChunkState::DataLf:
            if (c != '\n') return fail(ParseError::InvalidChunkTerminator);
            chunk_state_ = ChunkState::SizeLine;
            return true;
        case ChunkState::Trailers:
            if (trailer_cr_ != 0) {
                if (c != '\n') return fail(ParseError::InvalidTrailer);
                if (trailer_line_size_ == 0) return true;
                trailer_line_size_ = 0;
                trailer_cr_ = 0;
                return true;
            }
            if (c == '\r') {
                trailer_cr_ = 1;
                return true;
            }
            if (c == '\n' || static_cast<unsigned char>(c) < 0x20U)
                return fail(ParseError::InvalidTrailer);
            if (++trailer_line_size_ > 1024)
                return fail(ParseError::InvalidTrailer);
            return true;
    }
    return fail(ParseError::InvalidChunkSize);
}

bool ResponseParser::finish(std::int64_t receive_monotonic_ns) noexcept {
    if (complete_) return true;
    complete_ = true;
    complete_ns_ = receive_monotonic_ns;
    return true;
}

bool ResponseParser::consume(
    std::span<const char> bytes,
    std::int64_t receive_monotonic_ns) noexcept {
    if (error_ != ParseError::None) return false;
    if (bytes.empty()) return true;
    if (receive_monotonic_ns <= 0
        || (last_receive_ns_ > 0 && receive_monotonic_ns < last_receive_ns_)) {
        return fail(ParseError::InvalidTimestamp);
    }
    if (complete_) return fail(ParseError::DataAfterComplete);
    if (first_byte_ns_ == 0) first_byte_ns_ = receive_monotonic_ns;
    last_receive_ns_ = receive_monotonic_ns;

    for (const char c : bytes) {
        if (!headers_complete_) {
            if (header_size_ >= header_.size())
                return fail(ParseError::HeaderTooLarge);
            header_[header_size_++] = c;
            constexpr std::string_view terminator = "\r\n\r\n";
            if (c == terminator[header_match_]) {
                ++header_match_;
                if (header_match_ == terminator.size()) {
                    if (!process_header()) return false;
                    if (framing_ == Framing::NoBody
                        || (framing_ == Framing::ContentLength
                            && content_length_ == 0)) {
                        if (!finish(receive_monotonic_ns)) return false;
                    }
                }
            } else {
                header_match_ = c == terminator[0] ? 1 : 0;
            }
            continue;
        }
        if (complete_) return fail(ParseError::DataAfterComplete);
        if (!consume_body_byte(c)) return false;
        if (framing_ == Framing::ContentLength
            && body_size_ == content_length_) {
            if (!finish(receive_monotonic_ns)) return false;
        } else if (framing_ == Framing::Chunked
                   && chunk_state_ == ChunkState::Trailers
                   && trailer_cr_ != 0
                   && trailer_line_size_ == 0
                   && c == '\n') {
            if (!finish(receive_monotonic_ns)) return false;
        }
    }
    return true;
}

ResponseSnapshot ResponseParser::snapshot() const noexcept {
    ResponseSnapshot out;
    out.status_code = status_code_;
    out.framing = framing_;
    out.error = error_;
    out.body_size = body_size_;
    out.first_byte_monotonic_ns = first_byte_ns_;
    out.complete_monotonic_ns = complete_ns_;
    out.first_byte_to_complete_ns = first_byte_ns_ > 0
        && complete_ns_ >= first_byte_ns_
        ? complete_ns_ - first_byte_ns_ : 0;
    out.headers_complete = headers_complete_ ? 1 : 0;
    out.complete = complete_ ? 1 : 0;
    out.http_success = complete_
        && error_ == ParseError::None
        && status_code_ >= 200 && status_code_ < 300 ? 1 : 0;
    out.body_captured = complete_ && error_ == ParseError::None ? 1 : 0;
    return out;
}

} // namespace pm::v7::clob_http_response
