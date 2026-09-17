# V7 native Exchange V2 EIP-712 hashing

This module implements the deterministic **hashing** stage for the current Polymarket CLOB Exchange V2 order format. It contains no private key and performs no signature or order submission.

Domain:

`EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)`

with name `Polymarket CTF Exchange` and version `2`.

Order:

`Order(uint256 salt,address maker,address signer,uint256 tokenId,uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,uint256 timestamp,bytes32 metadata,bytes32 builder)`

The standard order digest is:

`keccak256(0x1901 || domain_separator || order_struct_hash)`

`expiration` is deliberately absent because it is not part of the official Exchange V2 EIP-712 Order struct.

The implementation includes its own bounded Ethereum Keccak-256 permutation to avoid confusing Ethereum Keccak with NIST SHA3-256 and to avoid adding a runtime crypto dependency solely for hashing. The regression test checks the empty Keccak vector plus the exact domain separator and order contents hash embedded in the official `py-clob-client-v2` POLY_1271 test vector. The final standard EIP-712 digest is also frozen from an independent `eth-account` calculation of the same public fixture.

This still does **not** complete native order signing. EOA/proxy/Safe secp256k1 signing and the deposit-wallet POLY_1271/ERC-7739 wrapping remain distinct stages. They should be added only with official parity vectors and without introducing another execution owner.
