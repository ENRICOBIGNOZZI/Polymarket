        if (std::isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') {
            out << static_cast<char>(c);
        } else {
            out << '%' << std::setw(2) << std::setfill('0') << static_cast<int>(c);
        }
    }
    return out.str();
}

std::string csv_escape(std::string_view value) {
    if (value.find_first_of(",\"\n\r") == std::string_view::npos) return std::string(value);
    std::string out = "\"";
    for (const char c : value) {
        if (c == '"') out += "\"\"";
        else out += c;
    }
    out += '"';
    return out;
}

std::vector<std::string> parse_csv_row(std::string_view line) {
    std::vector<std::string> fields;
    std::string current;
    bool quoted = false;
    for (std::size_t i = 0; i < line.size(); ++i) {
        const char c = line[i];
        if (quoted) {
            if (c == '"' && i + 1 < line.size() && line[i + 1] == '"') {
                current.push_back('"');
                ++i;
            } else if (c == '"') {
                quoted = false;
            } else {
                current.push_back(c);
            }
        } else if (c == '"') {
            quoted = true;
        } else if (c == ',') {
            fields.push_back(trim(std::move(current)));
            current.clear();
        } else {
            current.push_back(c);
        }
    }
    fields.push_back(trim(std::move(current)));
    return fields;
}

void atomic_write(const std::filesystem::path& path, std::string_view content) {
    std::filesystem::create_directories(path.parent_path());
    const auto temporary = path.string() + ".tmp";
    {
        std::ofstream output(temporary, std::ios::trunc);
        if (!output) throw std::runtime_error("cannot write temporary file: " + temporary);
        output << content;
        output.flush();
        if (!output) throw std::runtime_error("failed writing temporary file: " + temporary);
    }
    std::filesystem::rename(temporary, path);
}

std::string venue_key(Venue venue, std::string_view market) {
    return std::string(to_string(venue)) + ':' + std::string(market);
}

bool identifier_matches(const VenueMarket& market, std::string_view identifier) {
    return market.market_id == identifier || market.slug == identifier || market.event_id == identifier ||
           market.yes_token == identifier || market.no_token == identifier;
}

const VenueMarket* find_market(
    const std::map<Venue, std::vector<VenueMarket>>& markets,
    Venue venue,
    std::string_view identifier) {
    const auto it = markets.find(venue);
    if (it == markets.end()) return nullptr;
    for (const auto& market : it->second) {
        if (identifier_matches(market, identifier)) return &market;
    }
    return nullptr;
}

VenueMarket from_polymarket(const Market& market) {
    VenueMarket out;
    out.venue = Venue::Polymarket;
    out.market_id = market.id;
    out.event_id = market.event_id;
    out.slug = market.slug;
    out.title = market.question;
    out.yes_token = market.yes_token;
    out.no_token = market.no_token;
    out.liquidity = market.liquidity;
    out.volume24h = market.volume24h;
    out.active = market.active && !market.closed && market.accepting_orders && market.enable_order_book;
    return out;
}

OutcomeBooks derive_complement_books(Book yes, std::string no_token) {
    OutcomeBooks out;
    out.yes = std::move(yes);
    out.no.token_id = std::move(no_token);
    out.no.tick_size = out.yes.tick_size;
    out.no.min_order_size = out.yes.min_order_size;
    out.no.last_trade = out.yes.last_trade > 0.0 ? 1.0 - out.yes.last_trade : 0.0;
    for (const auto& ask : out.yes.asks) {
        if (ask.price > 0.0 && ask.price < 1.0 && ask.size > 0.0)
            out.no.bids.push_back({1.0 - ask.price, ask.size});
    }
    for (const auto& bid : out.yes.bids) {
        if (bid.price > 0.0 && bid.price < 1.0 && bid.size > 0.0)
            out.no.asks.push_back({1.0 - bid.price, bid.size});
    }
    return out;
}

VenueMarket parse_limitless_market_object(const json::object& object) {
    VenueMarket market;
    market.venue = Venue::Limitless;
    market.market_id = object_string(object, "id");
    market.slug = object_string(object, "slug");
    market.title = object_string(object, "title");
    market.event_id = object_string(object, "negRiskMarketId");
    if (market.event_id.empty()) market.event_id = object_string(object, "conditionId");
    if (market.event_id.empty()) market.event_id = market.market_id;
    market.close_ts_ms = object_int64(object, "expirationTimestamp");
    market.liquidity = object_double(object, "liquidity");
    market.volume24h = object_double(object, "volume");
    market.active = !object_bool(object, "expired", false) &&
                    lower_ascii(object_string(object, "status", "active")) != "expired";
    market.rules_primary = object_string(object, "description");
    if (const auto* tokens = find_value(object, "tokens"); tokens && tokens->is_object()) {
        const auto& token_object = tokens->as_object();
        market.yes_token = object_string(token_object, "yes");
        market.no_token = object_string(token_object, "no");
    }
    return market;
}

double limitless_size(const json::value& value) {
    const double raw = json_double(value, 0.0);
    if (raw <= 0.0) return 0.0;
    if (value.is_int64() || value.is_uint64() || raw >= 100000.0) return raw / 1'000'000.0;
    return raw;
}

void parse_limitless_levels(const json::value* value, std::vector<Level>& levels) {
    if (value == nullptr || !value->is_array()) return;
    for (const auto& entry : value->as_array()) {
        if (!entry.is_object()) continue;
        const auto& object = entry.as_object();
        const double price = object_double(object, "price", -1.0);
        const auto* size_value = find_value(object, "size");
        const double size = size_value ? limitless_size(*size_value) : 0.0;
        if (price > 0.0 && price < 1.0 && size > 0.0) levels.push_back({price, size});
    }
}

void sort_books(OutcomeBooks& books) {
    auto sort_book = [](Book& book) {
        std::sort(book.bids.begin(), book.bids.end(), [](const Level& left, const Level& right) {
            return left.price > right.price;
        });
        std::sort(book.asks.begin(), book.asks.end(), [](const Level& left, const Level& right) {
            return left.price < right.price;
        });
    };
    sort_book(books.yes);
    sort_book(books.no);
}

std::vector<std::string> text_tokens(std::string_view value) {
    const std::string canonical = canonical_event_text(value);
    std::istringstream input(canonical);
    std::vector<std::string> tokens;
    std::string token;
    static const std::unordered_set<std::string> stop_words{
        "a", "an", "and", "are", "be", "by", "for", "in", "is", "of", "on", "or", "the",
        "to", "will", "with", "yes", "no", "before", "after"};
    while (input >> token) {
        if (!stop_words.contains(token)) tokens.push_back(std::move(token));
    }
    return tokens;
}

std::multiset<std::string> numeric_tokens(std::string_view value) {
    std::multiset<std::string> numbers;
    static const std::regex pattern(R"(([0-9]+(?:\.[0-9]+)?))");
    std::string text;
    text.reserve(value.size());
    for (std::size_t i = 0; i < value.size(); ++i) {
        const char c = value[i];
        if (c == ',' && i > 0 && i + 1 < value.size() &&
            std::isdigit(static_cast<unsigned char>(value[i - 1])) &&
            std::isdigit(static_cast<unsigned char>(value[i + 1]))) {
            continue;
        }
        text.push_back(c);
    }
    for (auto it = std::sregex_iterator(text.begin(), text.end(), pattern);
         it != std::sregex_iterator(); ++it) {
        numbers.insert((*it)[1].str());
    }
    return numbers;
}

struct BuyWalk {
    bool complete = false;
    double shares = 0.0;
    double cost = 0.0;
    double average_price = 0.0;
};

BuyWalk walk_buy(const Book& book, double requested_shares, double slippage_bps) {
    BuyWalk out;
    if (requested_shares <= 0.0) return out;
    auto asks = book.asks;
    std::sort(asks.begin(), asks.end(), [](const Level& left, const Level& right) {
        return left.price < right.price;
    });
    const double multiplier = 1.0 + std::max(0.0, slippage_bps) / 10000.0;
    double remaining = requested_shares;
    for (const auto& level : asks) {
        if (remaining <= kEpsilon) break;
        if (level.price <= 0.0 || level.price >= 1.0 || level.size <= 0.0) continue;
        const double shares = std::min(remaining, level.size);
        const double price = std::min(1.0, level.price * multiplier);
        out.cost += shares * price;
        out.shares += shares;
        remaining -= shares;
    }
    out.complete = remaining <= 1e-9;
    if (out.shares > 0.0) out.average_price = out.cost / out.shares;
    return out;
}

double available_ask_shares(const Book& book) {
    double result = 0.0;
    for (const auto& level : book.asks) result += std::max(0.0, level.size);
    return result;
}

const Book& outcome_book(const OutcomeBooks& books, Outcome outcome) {
    return outcome == Outcome::Yes ? books.yes : books.no;
}

struct PaperState {
    double cash = 0.0;
    double reserved_capital = 0.0;
    double expected_locked_profit = 0.0;
    double peak_equity = 0.0;
    bool killed = false;
    std::unordered_map<std::string, std::int64_t> last_pair_trade;
};

PaperState load_paper_state(const std::filesystem::path& path, double starting_capital) {
    PaperState state;
    state.cash = starting_capital;
    state.peak_equity = starting_capital;
    std::ifstream input(path);
    if (!input) return state;
    std::ostringstream buffer;
    buffer << input.rdbuf();
    try {
        const auto value = json::parse(buffer.str());
        if (!value.is_object()) return state;
        const auto& object = value.as_object();
        state.cash = object_double(object, "cash", starting_capital);
        state.reserved_capital = object_double(object, "reserved_capital");
        state.expected_locked_profit = object_double(object, "expected_locked_profit");
        state.peak_equity = object_double(object, "peak_equity", starting_capital);
        state.killed = object_bool(object, "killed", false);
        if (const auto* last = find_value(object, "last_pair_trade"); last && last->is_object()) {
            for (const auto& item : last->as_object()) {
                state.last_pair_trade[std::string(item.key())] = json_int64(item.value());
            }
        }
    } catch (...) {
    }
    return state;
}

void persist_paper_state(const std::filesystem::path& path, const PaperState& state) {
    json::object last;
    for (const auto& [pair, timestamp] : state.last_pair_trade) last[pair] = timestamp;
    json::object object;
    object["schema_version"] = 1;
    object["timestamp"] = now_seconds();
    object["cash"] = state.cash;
    object["reserved_capital"] = state.reserved_capital;
    object["expected_locked_profit"] = state.expected_locked_profit;
    object["peak_equity"] = state.peak_equity;
    object["killed"] = state.killed;
    object["last_pair_trade"] = std::move(last);
    atomic_write(path, json::serialize(object));
}

void append_bundle(const std::filesystem::path& path, const Opportunity& opportunity) {
    const bool new_file = !std::filesystem::exists(path) || std::filesystem::file_size(path) == 0;
    std::filesystem::create_directories(path.parent_path());
    std::ofstream output(path, std::ios::app);
