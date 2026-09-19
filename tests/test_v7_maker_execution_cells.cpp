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

void write_model(const std::filesystem::path& path, const std::string& sha,
                 bool include_identity = true) {
    std::ofstream out(path, std::ios::trunc);
    out << R"({
  "schema": "polymarket_v7_maker_execution_model_v1",
  "strategy": "CRYPTO_SETTLEMENT_ENGINE",
  "paper_only": true,
  "authenticated_execution": false,
  "real_order_submission": false,
)";
    if (include_identity) {
        out << R"(  "policy_hash": "a0c7f86875181980",
  "config_hash": "3935c59ce2037137",
  "execution_semantics_version": "maker-paper-v7.2-bilateral-inventory",
)";
    }
    out << R"(  "model_sha": ")" << sha << R"(",
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
    },
    "JOIN|NO|SELL": {
      "orders": 0,
      "filled_orders": 0,
      "event_clusters": 0,
      "fill_probability": 0.10,
      "adverse_markout_per_share": 0.01,
      "adverse_markout_observations": 0,
      "adverse_markout_event_clusters": 0
    }
  },
  "risk_only_symmetric_outcome_adverse": {
    "JOIN|SELL": {
      "adverse_markout_per_share": 0.05,
      "adverse_markout_observations": 1,
      "adverse_markout_event_clusters": 1
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
    const std::string loaded_hash(model.execution_artifact_sha256.data());
    assert(loaded_hash.size() == 64);
    assert(std::string(model.execution_policy_hash.data()) == "a0c7f86875181980");
    assert(std::string(model.execution_config_hash.data()) == "3935c59ce2037137");
    assert(std::string(model.execution_semantics_version.data())
        == "maker-paper-v7.2-bilateral-inventory");
    pm::v7::maker::MakerModelSnapshot same_bytes;
    assert(std::string(same_bytes.execution_artifact_sha256.data()) == loaded_hash);
    { std::ofstream append(path, std::ios::app); append << '\n'; }
    pm::v7::maker::MakerModelSnapshot changed_bytes;
    assert(std::string(changed_bytes.execution_artifact_sha256.data()).size() == 64);
    assert(std::string(changed_bytes.execution_artifact_sha256.data()) != loaded_hash);
    assert(close(changed_bytes.execution_cells[index].fill_probability, cell.fill_probability));
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

    const auto pooled_index = pm::v7::maker::execution_cell_index(
        pm::v7::maker::Action::Join, -1, pm::v7::Side::Sell);
    const auto& pooled = model.execution_cells[pooled_index];
    assert(pooled.valid);
    assert(close(pooled.adverse_markout_per_share, 0.05));
    assert(pooled.orders == 0);
    assert(pooled.filled_orders == 0);
    assert(pooled.adverse_markouts == 1);

    unsetenv("PM_V7_MAKER_EXECUTION_MODEL");
    unsetenv("PM_V7_MODEL_SHA");
    std::filesystem::remove(path);
}

void test_missing_execution_identity_fails_closed() {
    const std::string sha(40, 'e');
    const auto path = std::filesystem::temp_directory_path()
        / "pm_v7_execution_cells_missing_identity_test.json";
    write_model(path, sha, false);
    setenv("PM_V7_MODEL_SHA", sha.c_str(), 1);
    setenv("PM_V7_MAKER_EXECUTION_MODEL", path.c_str(), 1);

    pm::v7::maker::MakerModelSnapshot model;
    for (const auto& cell : model.execution_cells) assert(!cell.valid);
    assert(model.execution_artifact_sha256[0] == '\0');
    assert(model.execution_policy_hash[0] == '\0');
    assert(model.execution_config_hash[0] == '\0');
    assert(model.execution_semantics_version[0] == '\0');

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
    assert(model.execution_artifact_sha256[0] == '\0');

    unsetenv("PM_V7_MAKER_EXECUTION_MODEL");
    unsetenv("PM_V7_MODEL_SHA");
    std::filesystem::remove(path);
}

} // namespace

int main() {
    test_exact_sha_loader_shrinks_to_global();
    test_missing_execution_identity_fails_closed();
    test_wrong_sha_fails_closed_to_invalid_cells();
    return 0;
}
