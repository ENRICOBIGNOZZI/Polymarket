#include "pm/v7_clob_prepared_order.hpp"

#include <cstring>
#include <limits>

namespace pm::v7::clob_wire {
namespace {

[[nodiscard]] bool json_atom(std::string_view value) noexcept {
    if (value.empty()) return false;
    for (const unsigned char c : value) {
        if (c < 0x20U || c > 0x7eU || c == '"' || c == '\\') return false;
    }
    return true;
}

[[nodiscard]] bool decimal(std::string_view value) noexcept {
    if (value.empty()) return false;
    for (const unsigned char c : value) {
        if (c < '0' || c > '9') return false;
    }
    return true;
}

class StaticWriter final {
public:
    explicit StaticWriter(std::span<char> output) noexcept : output_(output) {}

    bool append(std::string_view value) noexcept {
        if (!ok_ || value.size() > output_.size() - size_) {
            ok_ = false;
            return false;
        }
        if (!value.empty()) {
            std::memcpy(output_.data() + size_, value.data(), value.size());
            size_ += value.size();
        }
        return true;
    }

    bool quoted(std::string_view value) noexcept {
        return append("\"") && append(value) && append("\"");
    }

    [[nodiscard]] bool ok() const noexcept { return ok_; }
    [[nodiscard]] std::size_t size() const noexcept { return size_; }

private:
    std::span<char> output_;
    std::size_t size_ = 0;
    bool ok_ = true;
};

[[nodiscard]] bool add_size(std::size_t& total, std::size_t value) noexcept {
    if (value > std::numeric_limits<std::size_t>::max() - total) return false;
    total += value;
    return true;
}

} // namespace

PreparedMarketOrderJson::PreparedMarketOrderJson(
    const PreparedMarketOrderStaticView& fixed) noexcept {
    if (fixed.expiration != "0"
        || (fixed.side != "BUY" && fixed.side != "SELL")
        || fixed.signature_type > 3
        || (fixed.order_type != MarketOrderType::FAK
            && fixed.order_type != MarketOrderType::FOK)
        || !json_atom(fixed.builder)
        || !json_atom(fixed.maker)
        || !json_atom(fixed.metadata)
        || !json_atom(fixed.signer)
        || !json_atom(fixed.token_id)
        || !json_atom(fixed.owner)) {
        return;
    }

    StaticWriter w(static_bytes_);
    const auto close_chunk = [&](std::size_t index, std::size_t begin) noexcept {
        offsets_[index] = begin;
        sizes_[index] = w.size() - begin;
    };

    std::size_t begin = w.size();
    w.append("{\"deferExec\":false,\"order\":{\"builder\":");
    w.quoted(fixed.builder);
    w.append(",\"expiration\":"); w.quoted(fixed.expiration);
    w.append(",\"maker\":"); w.quoted(fixed.maker);
    w.append(",\"makerAmount\":\"");
    close_chunk(0, begin);

    begin = w.size();
    w.append("\",\"metadata\":"); w.quoted(fixed.metadata);
    w.append(",\"salt\":");
    close_chunk(1, begin);

    begin = w.size();
    w.append(",\"side\":"); w.quoted(fixed.side);
    w.append(",\"signature\":\"");
    close_chunk(2, begin);

    begin = w.size();
    w.append("\",\"signatureType\":");
    const char signature_type = static_cast<char>('0' + fixed.signature_type);
    w.append(std::string_view(&signature_type, 1));
    w.append(",\"signer\":"); w.quoted(fixed.signer);
    w.append(",\"takerAmount\":\"");
    close_chunk(3, begin);

    begin = w.size();
    w.append("\",\"timestamp\":\"");
    close_chunk(4, begin);

    begin = w.size();
    w.append("\",\"tokenId\":"); w.quoted(fixed.token_id);
    w.append("},\"orderType\":\"");
    w.append(fixed.order_type == MarketOrderType::FAK ? "FAK" : "FOK");
    w.append("\",\"owner\":"); w.quoted(fixed.owner); w.append("}");
    close_chunk(5, begin);

    if (!w.ok()) return;
    static_size_ = w.size();
    valid_ = true;
}

std::size_t PreparedMarketOrderJson::serialize(
    const MarketOrderDynamicView& dynamic,
    std::span<char> output) const noexcept {
    if (!valid_
        || !decimal(dynamic.maker_amount)
        || !decimal(dynamic.salt_decimal)
        || !json_atom(dynamic.signature)
        || !decimal(dynamic.taker_amount)
        || !decimal(dynamic.timestamp_ms)) {
        return 0;
    }

    std::size_t required = static_size_;
    if (!add_size(required, dynamic.maker_amount.size())
        || !add_size(required, dynamic.salt_decimal.size())
        || !add_size(required, dynamic.signature.size())
        || !add_size(required, dynamic.taker_amount.size())
        || !add_size(required, dynamic.timestamp_ms.size())
        || output.size() < required) {
        return 0;
    }

    std::size_t position = 0;
    const auto copy = [&](std::string_view value) noexcept {
        if (!value.empty()) {
            std::memcpy(output.data() + position, value.data(), value.size());
            position += value.size();
        }
    };
    const auto copy_chunk = [&](std::size_t index) noexcept {
        copy(std::string_view(static_bytes_.data() + offsets_[index], sizes_[index]));
    };

    copy_chunk(0); copy(dynamic.maker_amount);
    copy_chunk(1); copy(dynamic.salt_decimal);
    copy_chunk(2); copy(dynamic.signature);
    copy_chunk(3); copy(dynamic.taker_amount);
    copy_chunk(4); copy(dynamic.timestamp_ms);
    copy_chunk(5);
    return position == required ? required : 0;
}

} // namespace pm::v7::clob_wire
