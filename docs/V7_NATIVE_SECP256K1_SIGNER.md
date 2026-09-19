# Native Exchange V2 secp256k1 signer candidate

This candidate closes the gap between the native EIP-712 digest and the CLOB
wire path without implementing elliptic-curve crypto inside V7. Signing and
recovery-id semantics are delegated to Bitcoin Core's `libsecp256k1` recovery
module.

The hot call emits the Ethereum 65-byte form `r || s || v`, with `v=27+recid`.
Recovery ids outside 0/1 fail closed. Context construction, secret-key
validation and context randomization are cold-path. The runtime key is copied
into one single-owner signer and overwritten on destruction.

Parity is frozen against `eth-account 0.13.7 Account._sign_hash` for the public
non-secret test key integer 1 and the existing Exchange V2 EIP-712 fixture
digest. The regression also requires deterministic reuse and invalid-key
rejection.

London integration is separate. Ubuntu 24.04 provides `libsecp256k1-dev`; the
runtime bundle must carry the matching shared library before this candidate can
enter a PAPER order path.

## Development mechanism benchmark

Same Mac, fixed digest, after warm-up:
- `libsecp256k1` recoverable sign: about 20.6 us p50;
- Python `eth-account Account._sign_hash`: about 3.14 ms p50.

That is roughly 150x lower signing latency in this local mechanism test. It is
not exchange or order-ACK latency evidence.

## Linux compatibility

The candidate was also compiled and run inside a disposable `ubuntu:24.04`
container using the distribution package `libsecp256k1-dev` 0.2.0. The exact
`eth-account` parity regression passed. Repositories/runners without the optional
dependency report this candidate test as an explicit skip until runtime wiring
adds the package.
