# V7 native CLOB HTTP/1.1 order frame

The generic repository HTTP client is appropriate for cold/control work but is not a target HFT order path: it serializes through a mutex and rebuilds dynamic strings/header lists for each request.

This component is an isolated zero-authority candidate for the final transport boundary. It converts:

`exact signed /order JSON body + current CLOB L2 headers`

into one caller-buffer-owned HTTP/1.1 byte frame ready for a persistent TLS socket write.

It emits the current Level-2 header names:

- `POLY_ADDRESS`
- `POLY_SIGNATURE`
- `POLY_TIMESTAMP`
- `POLY_API_KEY`
- `POLY_PASSPHRASE`

plus fixed `POST /order`, host, JSON content type, content length and keep-alive framing. Header control characters are rejected to prevent request/header injection.

The exact body must be the same bytes used by the L2 HMAC signer. This module never parses, rewrites or reserializes that body.

This is **not** a transport implementation. It opens no socket and submits no order. HTTP/1.1 versus HTTP/2, TLS implementation, connection prewarming, write strategy and response parsing still require same-host London benchmarks before one is selected. The purpose here is only to remove dynamic request construction from that future hot path.
