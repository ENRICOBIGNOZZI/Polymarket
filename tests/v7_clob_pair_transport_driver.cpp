#include "pm/v7_clob_pair_transport.hpp"

#include <boost/json.hpp>
#include <array>
#include <cassert>
#include <cstdlib>
#include <iostream>
#include <string_view>

namespace json = boost::json;
using namespace pm::v7::clob;

namespace {
template <std::size_t N>
std::span<const char> view(const std::array<char,N>& data, std::size_t size) {
    return {data.data(),size};
}
}

int main(int argc,char**argv) {
    if(argc!=3) return 64;
    const auto port=static_cast<std::uint16_t>(
        std::strtoul(argv[1],nullptr,10));
    PairPersistentTlsTransport transport("localhost",port,2'000);
    const auto connected=transport.connect(argv[2]);
    assert(connected.ready && transport.ready());

    constexpr std::string_view yes=
        "POST /order HTTP/1.1\r\nHost: localhost\r\n"
        "Content-Length: 3\r\nConnection: keep-alive\r\n\r\nyes";
    constexpr std::string_view no=
        "POST /order HTTP/1.1\r\nHost: localhost\r\n"
        "Content-Length: 2\r\nConnection: keep-alive\r\n\r\nno";
    constexpr std::string_view batch=
        "POST /orders HTTP/1.1\r\nHost: localhost\r\n"
        "Content-Length: 8\r\nConnection: keep-alive\r\n\r\n[yes,no]";

    const auto pair=transport.submit_parallel(
        {yes.data(),yes.size()},{no.data(),no.size()});
    assert(pair.both_wire_ok);
    assert(pair.both_response_ok);
    assert(pair.yes.http_status==200);
    assert(pair.no.http_status==200);
    assert(pair.yes.wire_complete_monotonic_ns>=pair.yes.write_start_monotonic_ns);
    assert(pair.no.wire_complete_monotonic_ns>=pair.no.write_start_monotonic_ns);
    assert(pair.yes.ack_complete_monotonic_ns>=pair.yes.wire_complete_monotonic_ns);
    assert(pair.no.ack_complete_monotonic_ns>=pair.no.wire_complete_monotonic_ns);
    assert(pair.wire_skew_ns>=0 && pair.ack_skew_ns>=0);

    const auto one=transport.submit_batch({batch.data(),batch.size()});
    assert(one.wire_ok && one.response_ok && one.http_status==200);
    assert(one.ack_complete_monotonic_ns>=one.wire_complete_monotonic_ns);

    std::cout<<json::serialize(json::object{
        {"schema","polymarket_v7_clob_pair_transport_test_v1"},
        {"paper_only",true},
        {"authenticated_execution",false},
        {"real_order_submission",false},
        {"parallel_wire_skew_ns",pair.wire_skew_ns},
        {"parallel_ack_skew_ns",pair.ack_skew_ns},
        {"parallel_yes_write_to_ack_ns",
            pair.yes.ack_complete_monotonic_ns-pair.yes.write_start_monotonic_ns},
        {"parallel_no_write_to_ack_ns",
            pair.no.ack_complete_monotonic_ns-pair.no.write_start_monotonic_ns},
        {"batch_write_to_ack_ns",
            one.ack_complete_monotonic_ns-one.write_start_monotonic_ns}
    })<<'\n';
    transport.close();
    return 0;
}
