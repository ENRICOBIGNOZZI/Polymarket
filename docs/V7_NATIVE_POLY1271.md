# Native POLY_1271 / ERC-7739 order signature boundary

Exchange V2 deposit-wallet orders do not sign the ordinary Order EIP-712 digest.
The official client hashes a Solady `TypedDataSign(Order contents,...)` envelope,
signs that digest, then returns an ERC-7739-style payload containing the inner
65-byte signature, Polymarket app-domain separator, Order contents hash, the
literal Order type string and its two-byte length.

This module keeps that mode separate from EOA/Proxy/Safe signing. A prepared
hasher freezes chain id, deposit-wallet signer and app-domain separator; each
order patches only the Order contents hash. Wrapping is caller-buffer-only.

The regression freezes the independent Python `eth_abi`/`eth_utils` digest and
reconstructs the exact public `EXPECTED_POLY_1271_SIGNATURE` fixture from the
current official `py-clob-client-v2` tests. No private key, credentials, network
or execution authority are involved.

## Development mechanism benchmark

Prepared POLY_1271 digest (contents hash -> Solady typed-data digest), 100k calls
on the contended development Mac: about 1.5 us p50. This is compute-only and not
network/order/ACK evidence.
