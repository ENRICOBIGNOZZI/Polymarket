# Native secp256k1 identity binding

A valid secp256k1 key is not sufficient execution identity evidence. Before any native signer is wired to an Exchange V2 order lane, the runtime must prove that the loaded key owns the configured Ethereum signer address.

This module performs that cold-path proof without retaining the key:

1. validate the secret scalar with libsecp256k1;
2. derive the uncompressed secp256k1 public key;
3. Ethereum-Keccak the 64-byte `x || y` payload;
4. take the final 20 bytes as the Ethereum address;
5. compare raw address bytes with the configured signer address.

For Exchange V2 signature types EOA, POLY_PROXY and POLY_GNOSIS_SAFE, this derived address must match `order.signer` before authority can be enabled.

For POLY_1271, the deposit-wallet address stored in `order.signer` is not the inner EOA key address. The inner signing authority therefore needs a separate explicit address binding; the two identities must not be conflated.

The code is zero-authority and performs no signing, networking or order submission.
