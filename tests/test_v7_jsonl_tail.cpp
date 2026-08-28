#include "pm/v7_jsonl_tail.hpp"

#include <cassert>
#include <string_view>

int main() {
    using pm::v7::complete_jsonl_prefix_size;
    assert(complete_jsonl_prefix_size("") == 0);
    assert(complete_jsonl_prefix_size("{\"a\":1}") == 0);
    assert(complete_jsonl_prefix_size("{\"a\":1}\n") == 8);

    constexpr std::string_view mixed = "{\"a\":1}\n{\"b\":2}\n{\"partial\":";
    const auto complete = complete_jsonl_prefix_size(mixed);
    assert(complete == std::string_view("{\"a\":1}\n{\"b\":2}\n").size());
    assert(mixed.substr(complete) == "{\"partial\":");

    constexpr std::string_view later = "{\"partial\":3}\n{\"tail\":4}";
    assert(complete_jsonl_prefix_size(later) == std::string_view("{\"partial\":3}\n").size());
    return 0;
}
