#include "pm/v7_clob_http_frame.hpp"

#include <array>
#include <charconv>
#include <cstring>

namespace pm::v7::clob_http_frame {
namespace {
class Writer final {
public:
    explicit Writer(std::span<char> out) noexcept : out_(out) {}
    bool append(std::string_view s) noexcept {
        if (!ok_ || s.size() > out_.size() - size_) { ok_ = false; return false; }
        if (!s.empty()) std::memcpy(out_.data() + size_, s.data(), s.size());
        size_ += s.size();
        return true;
    }
    bool decimal(std::size_t value) noexcept {
        std::array<char, 32> buffer{};
        const auto [end, ec] = std::to_chars(buffer.data(), buffer.data() + buffer.size(), value);
        return ec == std::errc{} && append(std::string_view(buffer.data(), static_cast<std::size_t>(end - buffer.data())));
    }
    [[nodiscard]] std::size_t size() const noexcept { return ok_ ? size_ : 0; }
private:
    std::span<char> out_;
    std::size_t size_ = 0;
    bool ok_ = true;
};

bool header_value(std::string_view value) noexcept {
    if (value.empty()) return false;
    for (const unsigned char c : value) {
        // Reject all controls including CR/LF. Visible ASCII and spaces are
        // sufficient for current CLOB addresses/keys/passphrases/signatures.
        if (c < 0x20U || c > 0x7eU) return false;
    }
    return true;
}

bool decimal_value(std::string_view value) noexcept {
    if (value.empty()) return false;
    for (const unsigned char c : value) if (c < '0' || c > '9') return false;
    return true;
}
}

std::size_t serialize_post_order_http1(const L2AuthHeadersView& auth,
                                       std::string_view exact_body,
                                       std::span<char> output) noexcept {
    if (exact_body.empty()
        || !header_value(auth.address)
        || !header_value(auth.signature)
        || !decimal_value(auth.timestamp)
        || !header_value(auth.api_key)
        || !header_value(auth.passphrase)) return 0;

    Writer w(output);
    w.append("POST /order HTTP/1.1\r\n");
    w.append("Host: clob.polymarket.com\r\n");
    w.append("Content-Type: application/json\r\n");
    w.append("Accept: application/json\r\n");
    w.append("POLY_ADDRESS: "); w.append(auth.address); w.append("\r\n");
    w.append("POLY_SIGNATURE: "); w.append(auth.signature); w.append("\r\n");
    w.append("POLY_TIMESTAMP: "); w.append(auth.timestamp); w.append("\r\n");
    w.append("POLY_API_KEY: "); w.append(auth.api_key); w.append("\r\n");
    w.append("POLY_PASSPHRASE: "); w.append(auth.passphrase); w.append("\r\n");
    w.append("Content-Length: "); w.decimal(exact_body.size()); w.append("\r\n");
    w.append("Connection: keep-alive\r\n\r\n");
    w.append(exact_body);
    return w.size();
}

} // namespace pm::v7::clob_http_frame
