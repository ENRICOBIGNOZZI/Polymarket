// ABI selector reference for the read-only NegRisk collector. Uses the existing
// Ethereum Keccak implementation, never NIST SHA3 or a new hashing dependency.
#include "pm/v7_clob_eip712.hpp"
#include <array>
#include <iostream>
#include <string_view>

int main(int argc, char** argv) {
    using namespace pm::v7::clob_eip712;
    if (argc>1) {
        for (int i=1;i<argc;++i) {
            std::array<char,64> hex{};
            if (!hash32_hex(keccak256(argv[i]),hex)) return 1;
            std::cout<<argv[i]<<' ';std::cout.write(hex.data(),64);std::cout<<'\n';
        }
        return 0;
    }
    for (const std::string_view signature : {
        "NEG_RISK_ADAPTER()", "WRAPPED_COLLATERAL()", "CONDITIONAL_TOKENS()", "COLLATERAL_TOKEN()", "USDCE()",
        "ctf()", "col()", "wcol()", "getMarketData(bytes32)", "getQuestionCount(bytes32)",
        "getFeeBips(bytes32)", "getConditionId(bytes32)", "getPositionId(bytes32,bool)",
        "getOutcomeSlotCount(bytes32)", "payoutDenominator(bytes32)", "payoutNumerators(bytes32,uint256)",
        "paused(address)"}) {
        const auto hash = keccak256(signature);
        std::array<char, 64> hex{};
        if (!hash32_hex(hash, hex)) return 1;
        std::cout << signature << ' '; std::cout.write(hex.data(), 8); std::cout << '\n';
    }
}
