# Crypto runtime boundary

`main` contains one live economic engine: `CRYPTO_SETTLEMENT_ENGINE`.
The authoritative London deployment surface is `deploy/london/runtime_manifest.json`.
Source code may remain in shared `src/`, `include/`, `scripts/` and `config/` trees, but only files reachable from that manifest can enter the London image.
Training, backtests and retrospective analysis live under the research boundary and are forbidden from the London launcher/image.
