#pragma once

#include "poly/types.hpp"
#include <string>
#include <unordered_map>
#include <vector>

namespace poly {

struct ClientConfig {
    std::size_t gamma_page_size{100};
    std::size_t max_markets{5000};
    std::size_t book_batch_size{100};
    double min_liquidity_for_book{25.0};
    std::size_t warm_start_tokens{600};
    std::size_t history_batch_size{20};
    int history_fidelity_min{1};
};

class PolymarketClient {
public:
    explicit PolymarketClient(ClientConfig cfg = {});
    std::unordered_map<std::string, TokenMeta> discover_tokens() const;
    std::unordered_map<std::string, BookSnapshot> fetch_books(const std::unordered_map<std::string, TokenMeta>& tokens) const;
    std::unordered_map<std::string, std::vector<HistoricalPoint>> fetch_warm_history(const std::unordered_map<std::string, TokenMeta>& tokens) const;

private:
    ClientConfig cfg_;
    static std::string http_get(const std::string& url);
    static std::string http_post_json(const std::string& url, const std::string& body);
};

} // namespace poly
