#pragma once
#include <array>
#include <cstdint>
#include <string_view>

namespace pm::v7 {
// One market/capture, ordered observations. Bounded memory, no hot allocation.
// Counts observed signals, not all exchange triggers or generation validity.
struct NativeSignalFunnel {
    inline static constexpr std::array<std::string_view, 8> names{
        "unique_signals_observed", "signals_valid_at_decision", "signals_expired_before_first_decision",
        "signals_in_tte_window", "signals_book_pretrigger_valid", "signals_probability_admissible",
        "signals_economically_positive", "signals_accepted"};
    std::array<std::uint64_t, 8> counts{};
    std::uint64_t last_version = 0;
    std::int64_t last_trigger = 0;
    std::uint16_t stages = 0;
    void observe(std::uint64_t version, std::int64_t trigger, std::int64_t decision,
                 std::int64_t expiry, std::int64_t close, std::int64_t book_receive,
                 bool valid_signal, bool valid_book, bool probability, bool positive_ev,
                 bool accepted) noexcept {
        if (!version || trigger <= 0 || decision < trigger) return;
        // Delayed observations cannot recount an already finished signal.
        if (trigger < last_trigger || (trigger == last_trigger && version < last_version)) return;
        if (trigger != last_trigger || version != last_version) {
            last_trigger = trigger; last_version = version; stages = 0;
            ++counts[0];
            if (expiry > 0 && decision > expiry) ++counts[2];
        }
        const auto mark = [&](std::size_t i, bool condition) {
            const auto bit = static_cast<std::uint16_t>(1U << i);
            if (condition && !(stages & bit)) { ++counts[i]; stages |= bit; }
        };
        mark(1, valid_signal && expiry >= decision);
        mark(3, close > decision && close-decision >= 105'000'000'000LL
                            && close-decision <= 120'000'000'000LL);
        mark(4, valid_book && book_receive > 0 && book_receive <= trigger);
        mark(5, probability);
        mark(6, probability && positive_ev);
        mark(7, accepted);
    }
};
} // namespace pm::v7
