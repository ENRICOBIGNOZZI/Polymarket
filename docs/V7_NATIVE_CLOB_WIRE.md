# V7 native CLOB wire boundary

This component covers only the final deterministic transformation from an **already signed** CLOB market order to the exact `/order` request body and CLOB L2 HMAC header signature.

It has zero execution authority. It does not hold a wallet private key, create an EIP-712 order signature, open a socket, or submit an order.

The current Polymarket direct API contract signs the exact serialized body with:

`request_timestamp + "POST" + "/order" + exact_body`

using HMAC-SHA256 with the base64-decoded CLOB API secret, then URL-safe base64 with padding. Because the HMAC covers the exact bytes transmitted, serialization and L2 signing belong in one deterministic native boundary.

`serialize_post_market_order()` is caller-buffer-only and allocation-free. The hot-path scope is deliberately narrow: FAK/FOK market orders with `expiration="0"`. It does not perform price discovery or REST book reads.

`L2HmacSigner` decodes the L2 secret and creates its OpenSSL MAC context during construction. `sign()` reuses that context and updates it from four spans without application-side concatenation of the HMAC message. This contract does not claim anything about OpenSSL internal allocation behavior.

The EIP-712 wallet/order signature remains a separate missing native stage. This component must not be described as a complete native order adapter until that signing stage and the persistent transport are implemented and measured.
