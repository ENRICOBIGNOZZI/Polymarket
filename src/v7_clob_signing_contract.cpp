#include "pm/v7_clob_signing_contract.hpp"

namespace pm::v7::clob_signing {
namespace {

[[nodiscard]] bool hex_char(char c) noexcept {
    return (c >= '0' && c <= '9')
        || (c >= 'a' && c <= 'f')
        || (c >= 'A' && c <= 'F');
}

} // namespace

bool parse_signature_mode(std::string_view text, SignatureMode& output) noexcept {
    if (text == "EOA") output = SignatureMode::Eoa;
    else if (text == "POLY_PROXY") output = SignatureMode::PolyProxy;
    else if (text == "POLY_GNOSIS_SAFE") output = SignatureMode::PolyGnosisSafe;
    else if (text == "POLY_1271") output = SignatureMode::Poly1271;
    else return false;
    return true;
}

bool valid_eth_address(std::string_view address) noexcept {
    if (address.size() != 42 || address[0] != '0'
        || (address[1] != 'x' && address[1] != 'X')) return false;
    for (std::size_t i = 2; i < address.size(); ++i) {
        if (!hex_char(address[i])) return false;
    }
    return true;
}
SigningContract build_signing_contract(SignatureMode mode,
                                       std::string_view maker,
                                       std::string_view key_signer) noexcept {
    SigningContract output;
    output.mode = mode;
    output.maker = maker;
    output.key_signer = key_signer;
    if (!valid_eth_address(maker) || !valid_eth_address(key_signer)) return output;

    switch (mode) {
        case SignatureMode::Eoa:
        case SignatureMode::PolyProxy:
        case SignatureMode::PolyGnosisSafe:
            output.order_signer = key_signer;
            break;
        case SignatureMode::Poly1271:
            output.order_signer = maker;
            output.requires_poly1271_wrap = 1;
            break;
        default:
            return output;
    }
    output.valid = 1;
    return output;
}

} // namespace pm::v7::clob_signing
