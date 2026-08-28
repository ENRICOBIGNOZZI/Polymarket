#pragma once

#include <cstddef>
#include <string_view>

namespace pm::v7 {

// Return the byte count of the largest newline-terminated JSONL prefix.
// Readers of a concurrently appended canonical ledger must never advance past
// an unterminated final record: the next poll must reread those bytes once the
// single writer completes the line.
[[nodiscard]] inline std::size_t complete_jsonl_prefix_size(
    std::string_view bytes) noexcept {
    const auto last_newline = bytes.rfind('\n');
    return last_newline == std::string_view::npos ? 0 : last_newline + 1;
}

} // namespace pm::v7
