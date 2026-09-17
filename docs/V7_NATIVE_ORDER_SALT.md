# V7 Native Exchange V2 Order Salt

The official Python V2 builder generates a fresh integer salt for each order.
The native reaction path must not call Python or a system RNG per decision.

`OrderSaltSequence` reads 64 bits from the operating-system CSPRNG once during
cold startup. The single order owner then advances through nonzero uint64 values
without syscalls, allocation, locks or atomics. Exchange V2 signs a uint256 salt,
so the uint64 value is represented exactly.

Within one initialized sequence no salt repeats until the entire nonzero uint64
space is exhausted. Across independent restarts/processes, collision resistance
comes from the 64-bit OS-random starting seed. The production design has one
canonical order-TX owner; it should own one sequence rather than one per strategy.

This component generates public order identity, not secret material. It has no
private key, credentials, signing, network or execution authority.
