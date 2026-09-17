# V7 CLOB signing contract

The Exchange V2 signing mode must be explicit before a native signer is allowed to own the order path.

Official V2 values mirrored here:
- `EOA = 0`;
- `POLY_PROXY = 1`;
- `POLY_GNOSIS_SAFE = 2`;
- `POLY_1271 = 3`.

For EOA, proxy and Safe modes, the Exchange V2 `order.signer` is the EOA key address. The maker/funder may differ for proxy/Safe accounts.

For `POLY_1271`, the high-level official builder places the funder/deposit-wallet address in `order.signer` and uses a distinct DepositWallet/Solady wrapping flow. A plain EIP-712 EOA signature is therefore not interchangeable with type 3.

This module owns no key and performs no cryptography. It only freezes the address/signature-mode contract so future native signing cannot silently choose the wrong format.

Runtime integration must fail closed if the wallet mode is absent or unsupported. No default wallet mode should be inferred from a test fixture.
