# V7 dynamic RX/NAPI affinity

The native crypto shadow path can align each feed thread to the CPU that
actually processed its latest TCP packet instead of trusting static placement.
Linux exposes receive lineage through `SO_INCOMING_CPU`; the associated
`SO_INCOMING_NAPI_ID` is recorded as evidence.

The decision CPU is never eligible. Only the three feed CPUs selected by the
HFT role allocator are passed as the dynamic RX allowlist. If the kernel reports
another CPU, the move is rejected and the run cannot qualify as clean latency
evidence.

Alignment happens after the first packet of each connection and again only if
the observed receive CPU changes. Reconnects therefore follow a new RSS/RX
placement without adding an affinity syscall to every frame.
