#include "pm/v7_hot_book_cache.hpp"

#include <cassert>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <unistd.h>

using namespace pm::v7;

namespace {
template <class T> T read_at(const std::string& bytes, std::size_t offset) {
    assert(offset + sizeof(T) <= bytes.size());
    T value{}; std::memcpy(&value, bytes.data() + offset, sizeof(T)); return value;
}
}

int main() {
    const auto path = std::filesystem::temp_directory_path()
        / ("v7-hot-book-" + std::to_string(::getpid()) + ".bin");
    {
        HotBookCacheWriter writer(path, std::string(40, 'a'), 2);
        BookHotSnapshot book;
        book.state_version = 77;
        book.exchange_event_ns = 1'789'650'000'123'000LL;
        book.receive_monotonic_ns = 123'456'789LL;
        book.tick_size_e4 = 10;
        book.best_bid_e4 = 4990; book.best_ask_e4 = 5010;
        book.best_bid_microunits = 3'000'000; book.best_ask_microunits = 4'000'000;
        book.bid_levels[0] = {4990, 3'000'000}; book.bid_levels[1] = {4980, 5'000'000};
        book.ask_levels[0] = {5010, 4'000'000}; book.ask_levels[1] = {5020, 6'000'000};
        book.bid_level_count = 2; book.ask_level_count = 2;
        book.lineage_continuous = 1; book.valid = 1;
        assert(writer.publish(1, "4618098", "82557822733541051116003757150499858511", book,
                              1'789'650'000'124LL, 5'000'000));
        assert(writer.publications() == 1 && writer.failures() == 0);
        assert(!writer.publish(0, "m", "t", book, 1, 1'000'000));
        assert(writer.failures() == 1);
    }
    std::ifstream input(path, std::ios::binary);
    std::string bytes((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
    assert(bytes.size() == kHotBookCacheHeaderBytes + 3 * kHotBookCacheSlotBytes);
    assert(bytes.substr(0, 8) == "V7HBK001");
    assert(read_at<std::uint32_t>(bytes, 8) == 1);
    assert(read_at<std::uint32_t>(bytes, 12) == 2);
    assert(bytes.substr(16, 40) == std::string(40, 'a'));
    const auto base = kHotBookCacheHeaderBytes + kHotBookCacheSlotBytes;
    assert(read_at<std::uint64_t>(bytes, base) == 2); // even committed sequence
    assert(read_at<std::uint64_t>(bytes, base + 8) == 1);
    assert(read_at<std::uint64_t>(bytes, base + 16) == 77);
    assert(read_at<std::int32_t>(bytes, base + 48) == 10);
    assert(static_cast<unsigned char>(bytes[base + 52]) == 2);
    assert(static_cast<unsigned char>(bytes[base + 53]) == 2);
    assert(read_at<std::int32_t>(bytes, base + 256) == 4990);
    assert(read_at<std::int64_t>(bytes, base + 260) == 3'000'000);
    assert(read_at<std::int32_t>(bytes, base + 376) == 5010);
    assert(read_at<std::int64_t>(bytes, base + 380) == 4'000'000);
    assert(read_at<std::int64_t>(bytes, base + 496) == 5'000'000);
    std::filesystem::remove(path);
    std::cout << "hot book cache writer PASS\n";
}
