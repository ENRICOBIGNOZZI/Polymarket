#include "pm/v7_latest_json_publisher.hpp"

#include <boost/json.hpp>

#include <cassert>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <unistd.h>

using namespace pm::v7::external_fair;
namespace fs = std::filesystem;
namespace json = boost::json;

int main() {
    const auto root = fs::temp_directory_path()
        / ("pm-v7-latest-json-" + std::to_string(::getpid()));
    fs::remove_all(root);
    const auto path = root / "nested" / "status.json";

    LatestJsonPublisher publisher(path);
    const std::string payload(32 * 1024, 'x');
    for (std::int64_t sequence = 1; sequence <= 100; ++sequence) {
        assert(publisher.publish(json::object{
            {"schema", "test_latest_json"},
            {"sequence", sequence},
            {"payload", payload},
        }));
    }
    publisher.close();

    const auto stats = publisher.snapshot();
    assert(stats.healthy == 1);
    assert(stats.failures == 0);
    assert(stats.pending == 0);
    assert(stats.in_flight == 0);
    assert(stats.submitted == 100);
    assert(stats.written >= 1 && stats.written <= stats.submitted);
    assert(stats.written + stats.coalesced == stats.submitted);
    assert(!publisher.publish(json::object{{"sequence", 101}}));

    std::ifstream input(path, std::ios::binary);
    assert(input.good());
    std::string text((std::istreambuf_iterator<char>(input)), {});
    boost::system::error_code error;
    const auto value = json::parse(text, error);
    assert(!error && value.is_object());
    assert(value.as_object().at("sequence").as_int64() == 100);

    fs::remove_all(root);
    std::cout << "v7 latest JSON publisher tests passed\n";
    return 0;
}
