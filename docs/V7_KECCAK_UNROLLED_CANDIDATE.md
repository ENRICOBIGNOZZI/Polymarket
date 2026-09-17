# V7 Portable Unrolled Ethereum Keccak Candidate

Exchange V2 EIP-712 requires Ethereum Keccak-256 twice per order: once for the
384-byte Order struct and once for the 66-byte EIP-712 envelope. The current
portable implementation is correct but its F1600 permutation uses indexed and
modulo-heavy inner loops.

This candidate preserves the exact Ethereum Keccak sponge, padding and round
constants while explicitly unrolling theta lane updates, rho/pi movement and chi
rows. It also uses native little-endian 64-bit loads/stores through `memcpy` on
little-endian hosts, with a portable byte fallback.

It is intentionally a separate function first. Promotion into the canonical
`keccak256()` path requires parity and same-host A/B evidence; no signing or
execution semantics are changed by this candidate.

Development Mac A/B before publication, 100,000 384-byte hashes under substantial
concurrent agent/build load:

- existing portable implementation: about 7.25 us p50;
- unrolled candidate: about 1.75 us p50.

The same temporary candidate matched the existing verified implementation for
boundary and multi-block lengths before being moved into this isolated module.
The repository regression now compares every input length from 0 through 1024
bytes and additionally checks the canonical Ethereum Keccak hashes for empty
input and `abc`.

These numbers are local compute evidence only, not London or order-latency claims.
