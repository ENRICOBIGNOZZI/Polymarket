# V7 Native CLOB Persistent TLS Transport

This component owns one persistent TLS connection and nothing else.

Cold path:

`DNS -> TCP connect -> certificate-verified TLS -> HTTP/1.1 ALPN`

Warm path:

`caller-owned bytes -> SSL_write_ex -> caller-owned response buffer`

The transport intentionally does not parse HTTP responses, create CLOB headers,
sign EIP-712 orders, own credentials, submit orders, mutate OMS state, or perform
retry/reconciliation policy. Those remain separate boundaries.

The socket enables TCP_NODELAY and keepalive. Certificate hostname verification,
SNI and peer-chain validation are mandatory. The hot read/write API performs no
C++ heap allocation; OpenSSL may use its own internal allocations.

Development validation uses a local certificate-authorized TLS server and two
HTTP/1.1 keep-alive requests over the same accepted TCP/TLS connection. Public
CLOB validation, when run manually, must use only unauthenticated `/time` or
`/ok`; this component is not wired to `/order` or any execution authority.
