#include "pm/v7_clob_http1_response.hpp"

#include <cmath>
#include <cstring>
#include <limits>

namespace pm::v7::clob_transport {
namespace {

[[nodiscard]] bool ascii_ieq(char a, char b) noexcept {
    const auto lower = [](unsigned char c) noexcept -> unsigned char {
        return (c >= 'A' && c <= 'Z') ? static_cast<unsigned char>(c + ('a' - 'A')) : c;
    };
    return lower(static_cast<unsigned char>(a)) == lower(static_cast<unsigned char>(b));
}

[[nodiscard]] bool iequals(std::string_view a, std::string_view b) noexcept {
    if (a.size() != b.size()) return false;
    for (std::size_t i = 0; i < a.size(); ++i) if (!ascii_ieq(a[i], b[i])) return false;
    return true;
}

[[nodiscard]] std::string_view trim_ows(std::string_view value) noexcept {
    while (!value.empty() && (value.front() == ' ' || value.front() == '\t')) value.remove_prefix(1);
    while (!value.empty() && (value.back() == ' ' || value.back() == '\t')) value.remove_suffix(1);
    return value;
}

[[nodiscard]] bool parse_decimal(std::string_view value, std::size_t& out) noexcept {
    value = trim_ows(value);
    if (value.empty()) return false;
    std::size_t result = 0;
    for (const unsigned char c : value) {
        if (c < '0' || c > '9') return false;
        const std::size_t digit = static_cast<std::size_t>(c - '0');
        if (result > (std::numeric_limits<std::size_t>::max() - digit) / 10) return false;
        result = result * 10 + digit;
    }
    out = result;
    return true;
}

[[nodiscard]] bool parse_signed_number(std::string_view value, double& out) noexcept {
    value = trim_ows(value);
    if (value.empty()) return false;
    bool negative = false;
    if (value.front() == '-') { negative = true; value.remove_prefix(1); }
    if (value.empty()) return false;
    double result = 0.0;
    bool any = false;
    while (!value.empty() && value.front() >= '0' && value.front() <= '9') {
        any = true;
        result = result * 10.0 + static_cast<double>(value.front() - '0');
        value.remove_prefix(1);
    }
    if (!value.empty() && value.front() == '.') {
        value.remove_prefix(1);
        double place = 0.1;
        while (!value.empty() && value.front() >= '0' && value.front() <= '9') {
            any = true;
            result += static_cast<double>(value.front() - '0') * place;
            place *= 0.1;
            value.remove_prefix(1);
        }
    }
    if (!any || !value.empty() || !std::isfinite(result)) return false;
    out = negative ? -result : result;
    return true;
}

[[nodiscard]] bool parse_i64(std::string_view value, std::int64_t& out) noexcept {
    value = trim_ows(value);
    if (value.empty()) return false;
    std::int64_t result = 0;
    for (const unsigned char c : value) {
        if (c < '0' || c > '9') return false;
        const auto digit = static_cast<std::int64_t>(c - '0');
        if (result > (std::numeric_limits<std::int64_t>::max() - digit) / 10) return false;
        result = result * 10 + digit;
    }
    out = result;
    return true;
}

[[nodiscard]] bool contains_token_ci(std::string_view value, std::string_view wanted) noexcept {
    while (!value.empty()) {
        const auto comma = value.find(',');
        const auto token = trim_ows(value.substr(0, comma));
        if (iequals(token, wanted)) return true;
        if (comma == std::string_view::npos) break;
        value.remove_prefix(comma + 1);
    }
    return false;
}

} // namespace

void FixedHttp1Response::reset() noexcept {
    size_ = 0;
    header_scan_from_ = 0;
    header_end_ = 0;
    content_length_ = 0;
    message_size_ = 0;
    status_code_ = 0;
    headers_parsed_ = false;
    connection_close_ = false;
    retry_after_seconds_ = 0;
    rate_limit_remaining_ = std::numeric_limits<double>::quiet_NaN();
    rate_limit_reset_unix_seconds_ = 0;
    rate_limit_tier_.fill(0);
    rate_limit_tier_size_ = 0;
    rate_limit_warning_ = false;
    state_ = Http1ResponseState::Receiving;
}

std::span<char> FixedHttp1Response::writable() noexcept {
    if (state_ != Http1ResponseState::Receiving || size_ >= storage_.size()) return {};
    return std::span<char>(storage_.data() + size_, storage_.size() - size_);
}

Http1ResponseState FixedHttp1Response::commit(std::size_t bytes_written) noexcept {
    if (state_ != Http1ResponseState::Receiving) return state_;
    if (bytes_written > storage_.size() - size_) {
        state_ = Http1ResponseState::Overflow;
        return state_;
    }
    size_ += bytes_written;

    if (!headers_parsed_) {
        bool found = false;
        const std::size_t begin = header_scan_from_;
        for (std::size_t i = begin; i + 3 < size_; ++i) {
            if (storage_[i] == '\r' && storage_[i + 1] == '\n'
                && storage_[i + 2] == '\r' && storage_[i + 3] == '\n') {
                header_end_ = i + 4;
                found = true;
                break;
            }
        }
        if (!found) {
            if (size_ > kMaxHeaderBytes) state_ = Http1ResponseState::Overflow;
            else header_scan_from_ = size_ > 3 ? size_ - 3 : 0;
            return state_;
        }
        state_ = parse_headers();
        if (state_ != Http1ResponseState::Receiving) return state_;
        headers_parsed_ = true;
    }

    if (size_ < message_size_) return state_;
    if (size_ > message_size_) {
        state_ = Http1ResponseState::Invalid;
        return state_;
    }
    state_ = Http1ResponseState::Complete;
    return state_;
}

Http1ResponseState FixedHttp1Response::parse_headers() noexcept {
    const std::string_view headers(storage_.data(), header_end_);
    const auto status_line_end = headers.find("\r\n");
    if (status_line_end == std::string_view::npos) return Http1ResponseState::Invalid;
    const auto status = headers.substr(0, status_line_end);
    if (status.size() < 12 || status.substr(0, 9) != "HTTP/1.1 ") return Http1ResponseState::UnsupportedFraming;
    if (status[9] < '1' || status[9] > '5' || status[10] < '0' || status[10] > '9'
        || status[11] < '0' || status[11] > '9') return Http1ResponseState::Invalid;
    status_code_ = (status[9] - '0') * 100 + (status[10] - '0') * 10 + (status[11] - '0');
    if (status_code_ >= 100 && status_code_ < 200) return Http1ResponseState::UnsupportedFraming;

    bool have_content_length = false;
    bool unsupported_transfer = false;
    std::size_t content_length = 0;
    std::size_t pos = status_line_end + 2;
    const std::size_t header_lines_end = header_end_ - 2;
    while (pos < header_lines_end) {
        const auto line_end = headers.find("\r\n", pos);
        if (line_end == std::string_view::npos || line_end > header_lines_end) return Http1ResponseState::Invalid;
        if (line_end == pos) break;
        const auto line = headers.substr(pos, line_end - pos);
        const auto colon = line.find(':');
        if (colon == std::string_view::npos || colon == 0) return Http1ResponseState::Invalid;
        const auto name = line.substr(0, colon);
        const auto value = trim_ows(line.substr(colon + 1));
        if (iequals(name, "Content-Length")) {
            std::size_t parsed = 0;
            if (!parse_decimal(value, parsed)) return Http1ResponseState::Invalid;
            if (have_content_length && parsed != content_length) return Http1ResponseState::Invalid;
            have_content_length = true;
            content_length = parsed;
        } else if (iequals(name, "Transfer-Encoding")) {
            if (!value.empty() && !iequals(value, "identity")) unsupported_transfer = true;
        } else if (iequals(name, "Connection")) {
            connection_close_ = contains_token_ci(value, "close");
        } else if (iequals(name, "Retry-After")) {
            std::size_t parsed = 0;
            if (parse_decimal(value, parsed)
                && parsed <= static_cast<std::size_t>(std::numeric_limits<int>::max())) {
                retry_after_seconds_ = static_cast<int>(parsed);
            }
        } else if (iequals(name, "Poly-RateLimit-Remaining")) {
            double parsed = 0.0;
            if (parse_signed_number(value, parsed)) rate_limit_remaining_ = parsed;
        } else if (iequals(name, "Poly-RateLimit-Reset")) {
            std::int64_t parsed = 0;
            if (parse_i64(value, parsed)) rate_limit_reset_unix_seconds_ = parsed;
        } else if (iequals(name, "Poly-RateLimit-Tier")) {
            if (!value.empty() && value.size() <= rate_limit_tier_.size()) {
                std::memcpy(rate_limit_tier_.data(), value.data(), value.size());
                rate_limit_tier_size_ = static_cast<std::uint8_t>(value.size());
            }
        } else if (iequals(name, "Poly-RateLimit-Warning")) {
            rate_limit_warning_ = iequals(value, "true");
        }
        pos = line_end + 2;
    }

    if (unsupported_transfer) return Http1ResponseState::UnsupportedFraming;
    const bool no_body_status = status_code_ == 204 || status_code_ == 304;
    if (!have_content_length && !no_body_status) return Http1ResponseState::UnsupportedFraming;
    content_length_ = have_content_length ? content_length : 0;
    if (content_length_ > storage_.size() - header_end_) return Http1ResponseState::Overflow;
    message_size_ = header_end_ + content_length_;
    return Http1ResponseState::Receiving;
}

std::string_view FixedHttp1Response::body() const noexcept {
    if (state_ != Http1ResponseState::Complete) return {};
    return std::string_view(storage_.data() + header_end_, content_length_);
}

} // namespace pm::v7::clob_transport
