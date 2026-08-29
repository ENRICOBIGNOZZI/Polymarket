#pragma once

#include <cstdint>
#include <iomanip>
#include <sstream>
#include <string>

namespace pm {

// Public Data API transactions can contain more than one fill. A transaction
// hash alone is therefore not a unique trade key. Include the economic fields
// so distinct assets/fills in the same transaction are retained while polling
// overlap remains idempotent.
inline std::string public_trade_key(const std::string& transaction_hash,
                                    const std::string& condition_id,
                                    const std::string& asset_id,
                                    std::int64_t timestamp,
                                    const std::string& side,
                                    double price,
                                    double size) {
    std::ostringstream os;
    os << transaction_hash << '|'
       << condition_id << '|'
       << asset_id << '|'
       << timestamp << '|'
       << side << '|'
       << std::setprecision(17) << price << '|'
       << std::setprecision(17) << size;
    return os.str();
}

} // namespace pm
