#pragma once

#include "pm/api.hpp"
#include "pm/types.hpp"
#include <filesystem>
#include <unordered_map>

namespace pm {

class MakerPaperController {
public:
    explicit MakerPaperController(Config cfg);
    static void apply_config(Config& cfg, const std::string& path);
    void run_once(bool allow_place_and_fill);

    static double quote_price(const Book& book, std::size_t improve_ticks);
    static double queue_ahead(const Book& book, double quote);

private:
    Config cfg_;
    PolymarketApi api_;
    std::unordered_map<std::string,MakerOrder> orders_;
    std::unordered_map<std::string,Position> positions_;
    double cash_ = 0.0;
    double peak_equity_ = 0.0;
    bool killed_ = false;

    void ensure_runtime() const;
    void load_state();
    void persist_state() const;
    std::vector<MakerCandidate> latest_candidates(const std::vector<Market>& markets,
                                                   const std::unordered_map<std::string,Book>& books) const;
    std::size_t process_pending(bool allow_fill);
    std::size_t place_quotes(std::vector<MakerCandidate> candidates, double equity, double gross, bool allow_place);
    void append_quote(const MakerCandidate& c) const;
    void append_fill(const Fill& f) const;
    double reserved_notional() const;
    double reserved_event(const std::string& event_id) const;
    double event_exposure(const std::string& event_id) const;
    double market_exposure(const std::string& market_id) const;
    static std::string csv_escape(const std::string& s);
};

} // namespace pm
