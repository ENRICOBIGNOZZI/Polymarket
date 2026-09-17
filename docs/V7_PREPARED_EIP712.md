# V7 Prepared Exchange V2 EIP-712 Hashing

The current general EIP-712 path accepts strings and reparses all order fields.
That is correct for cold/general use, but wasteful for the crypto reaction path.

For a fixed CLOB market and account, maker, signer, tokenId, side,
signatureType, metadata and builder are invariant. `ExchangeV2PreparedOrderHasher`
parses and ABI-encodes them once during cold setup. The hot decision patches only:

- salt
- makerAmount
- takerAmount
- timestamp

Those four fields are uint64 in the current official V2 construction. The tokenId
remains full uint256 and is parsed once at setup.

Per order the prepared path performs only two Keccak hashes: the already-encoded
384-byte Order struct and the 66-byte EIP-712 envelope. It is single-owner by
design because its fixed buffer is patched in place. No signing or private key is
part of this component.
