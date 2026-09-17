# V7 CLOB pre-wire benchmark

This benchmark measures the native CPU path immediately before the persistent TLS socket:

1. Exchange V2 amount construction;
2. order salt generation and decimal formatting;
3. prepared Exchange V2 EIP-712 digest;
4. deterministic `/order` JSON body serialization;
5. CLOB L2 HMAC;
6. HTTP/1.1 order frame serialization.

It intentionally excludes recoverable secp256k1 order signing and network/TLS/exchange latency. Those must be measured separately.

Run the standalone source against the branch being evaluated so every optimization is compared on the same composition, not on unrelated microbenchmarks.

Development-Mac reference after canonical unrolled Keccak promotion, before pending wire/HMAC sweep integration:
- amount + salt + decimal: ~0.04 us p50;
- EIP-712: ~1.29 us p50;
- JSON body: ~0.88 us p50;
- L2 HMAC: ~0.96 us p50;
- HTTP frame: ~0.17 us p50;
- total excluding secp256k1/socket: ~3.29 us p50.

These are development mechanism numbers, not London or exchange performance claims. Tail values on a contended Mac are not promotion evidence.
