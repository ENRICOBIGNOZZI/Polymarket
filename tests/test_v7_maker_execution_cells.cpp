#include "pm/v7_maker_hft.hpp"

#include <cassert>
#include <cmath>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <string>

namespace {

bool close(double a, double b, double tol = 1e-12) {
    return std::abs(a - b) <= tol;
}

void write_model(const std::filesystem::path& path, const std::string& sha) {
    std::ofstream out(path, std::ios::trunc);
    out << R"({
  "schema": "polymarket_v7_maker_execution_model_v1",
  "strategy": "MICRO_MAKER_PRO",
  "paper_only": true,
  "authenticated_execution": false,
  "model_sha": ")" << sha << R"(",
  "groups": {
    "GLOBAL": {
      "fill_probability": 0.10,
      "adverse_markout_per_share": 0.01
    },
    "IMPROVE1|YES|BUY": {
      "orders": 80,
      "filled_orders": 40,
      "event_clusters": 10,
      "fill_probability": 0.70,
      "adverse_markout_per_share": 0.03,
      "adverse_markout_observations": 20,
      "adverse_markout_event_clusters": 10
    },
    "FADE1|NO|SELL": {
      "orders": 20,
      "filled_orders": 2,
      "event_clusters": 2,
      "fill_probability": 0.02,
      "adverse_markout_per_share": 0.04,
      "adverse_markout_n": 0
    }
  }
})";
}

void test_exact_sha_loader_shrinks_to_global() {
    const std::string sha(40, 'b');
    const auto path = std::filesystem::temp_directory_path() / "pm_v7_execution_cells_test.json";
    write_model(path, sha);
    setenv("PM_V7_MODEL_SHA", sha.c_str(), 1);
    setenv("PM_V7_MAKER_EXECUTION_MODEL", path.c_str(), 1);

    pm::v7::maker::MakerModelSnapshot model;
    const auto index = pm::v7::maker::execution_cell_index(
        pm::v7::maker::Action::Improve1, 1, pm::v7::Side::Buy);
    assert(index < model.execution_cells.size());
    const auto& cell = model.execution_cells[index];
    assert(cell.valid);
    // orders: 80/(80+40)=2/3; clusters: 10/(10+5)=2/3.
    assert(close(cell.fill_weight, 2.0 / 3.0));
    assert(close(cell.fill_probability, 0.10 + (2.0 / 3.0) * 0.60));
    // markouts: 20/(20+20)=1/2, below the cluster weight.
    assert(close(cell.markout_weight, 0.5));
    // Durable learning already shrinks the posterior; the loader must not
    // shrink it a second time toward GLOBAL.
    assert(close(cell.adverse_markout_per_share, 0.03));
    assert(cell.orders == 80);
    assert(cell.filled_orders == 40);
    assert(cell.adverse_markouts == 20);
    assert(cell.event_clusters == 10);

    const auto zero_markout_index = pm::v7::maker::execution_cell_index(
        pm::v7::maker::Action::Fade1, -1, pm::v7::Side::Sell);
    const auto& zero_markout = model.execution_cells[zero_markout_index];
    assert(zero_markout.valid);
    assert(zero_markout.adverse_markouts == 0);
    assert(zero_markout.markout_weight == 0.0);

    unsetenv("PM_V7_MAKER_EXECUTION_MODEL");
    unsetenv("PM_V7_MODEL_SHA");
    std::filesystem::remove(path);
}

void test_wrong_sha_fails_closed_to_invalid_cells() {
    const std::string expected(40, 'c');
    const std::string stale(40, 'd');
    const auto path = std::filesystem::temp_directory_path() / "pm_v7_execution_cells_stale_test.json";
    write_model(path, stale);
    setenv("PM_V7_MODEL_SHA", expected.c_str(), 1);
    setenv("PM_V7_MAKER_EXECUTION_MODEL", path.c_str(), 1);

    pm::v7::maker::MakerModelSnapshot model;
    for (const auto& cell : model.execution_cells) assert(!cell.valid);

    unsetenv("PM_V7_MAKER_EXECUTION_MODEL");
    unsetenv("PM_V7_MODEL_SHA");
    std::filesystem::remove(path);
}

} // namespace

int main() {
    test_exact_sha_loader_shrinks_to_global();
    test_wrong_sha_fails_closed_to_invalid_cells();
    return 0;
}
