#include "pm/v7_clob_order_identity.hpp"

#include <cassert>
#include <cstdio>
#include <string>

using namespace pm::v7::clob_identity;

int main() {
    const auto live = decode_post_order_ack(
        R"({"success":true,"errorMsg":"","orderID":"0xabc123","takingAmount":"0","makingAmount":"5","status":"live"})");
    assert(live.valid && live.success && live.status_live);
    assert(live.exchange_order_id.view() == "0xabc123");

    const auto matched = decode_post_order_ack(
        R"({"success":true,"orderID":"0xmatched","status":"matched"})");
    assert(matched.valid && matched.success && matched.status_matched);

    const auto rejected = decode_post_order_ack(
        R"({"success":false,"errorMsg":"insufficient balance"})");
    assert(rejected.valid && !rejected.success);
    assert(rejected.error() == "insufficient balance");

    assert(!decode_post_order_ack(R"({"success":true,"orderID":"x","status":"future-status"})").valid);
    assert(!decode_post_order_ack("not-json").valid);

    OrderIdentityMap map;
    assert(map.bind("0xabc123", 11));
    assert(map.bind("0xabc123", 11));
    assert(!map.bind("0xabc123", 12));
    assert(!map.bind("0xother", 11));
    assert(map.lookup_client("0xabc123").found);
    assert(map.lookup_client("0xabc123").client_order_id == 11);
    const auto reverse = map.lookup_exchange(11);
    assert(reverse.found && reverse.exchange_order_id == "0xabc123");

    // Exercise many exact identities and tombstone reuse. No hash value itself
    // ever becomes an authoritative order identifier.
    for (std::uint64_t i = 100; i < 3'000; ++i) {
        char buffer[64]{};
        const int n = std::snprintf(buffer, sizeof(buffer), "0xorder-%llu",
                                    static_cast<unsigned long long>(i));
        assert(n > 0);
        assert(map.bind(std::string_view(buffer, static_cast<std::size_t>(n)), i));
    }
    assert(map.size() == 2'901);
    for (std::uint64_t i = 100; i < 3'000; i += 2) {
        const auto id = map.lookup_exchange(i);
        assert(id.found);
        assert(map.erase_client(i));
        assert(!map.lookup_exchange(i).found);
        assert(!map.lookup_client(id.exchange_order_id).found);
    }
    for (std::uint64_t i = 4'000; i < 5'000; ++i) {
        char buffer[64]{};
        const int n = std::snprintf(buffer, sizeof(buffer), "0xreplacement-%llu",
                                    static_cast<unsigned long long>(i));
        assert(n > 0);
        assert(map.bind(std::string_view(buffer, static_cast<std::size_t>(n)), i));
    }
    assert(map.erase_exchange("0xabc123"));
    assert(!map.lookup_client("0xabc123").found);
    return 0;
}
