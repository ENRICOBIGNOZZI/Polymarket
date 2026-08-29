#pragma once

#include "pm/types.hpp"

#include <string>

namespace pm {

// V7/common configuration loader. This is data/runtime configuration only;
// execution, broker state, ledger ownership and risk live in canonical V7.
Config load_config(const std::string& path);

} // namespace pm
