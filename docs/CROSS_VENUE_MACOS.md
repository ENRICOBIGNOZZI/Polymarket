# Cross-venue macOS services

The incumbent `com.polymarket.paper` launch daemon remains unchanged. The cross-venue system is installed as two additional, independent services:

```text
com.polymarket.portfolio-supervisor
com.polymarket.cross-venue
```

The supervisor starts first and must publish `runs/supervisor/capital_limits.json` before the cross-venue process starts. Neither service contains authenticated order-submission code.

## 1. Install local credentials

Keep credentials outside the repository:

```bash
cd "$HOME/polymarket"
bash scripts/install_cross_venue_credentials.sh
```

The installer preserves existing Limitless fields, asks for the Kalshi key ID and PEM, validates the RSA key, and applies mode `600`.

## 2. Build and test

```bash
cd "$HOME/polymarket"
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel "$(sysctl -n hw.logicalcpu 2>/dev/null || echo 2)"
ctest --test-dir build --output-on-failure
```

## 3. Install the independent services

```bash
cd "$HOME/polymarket"
bash ops/install_cross_venue_macos.sh install
```

Installation performs an authenticated Limitless/Kalshi health check before writing or bootstrapping the launch daemons. It does not print credentials, signatures, account identifiers, or private-key material.

## Operations

```bash
bash ops/install_cross_venue_macos.sh status
bash ops/install_cross_venue_macos.sh restart
bash ops/install_cross_venue_macos.sh stop
bash ops/install_cross_venue_macos.sh start
bash ops/install_cross_venue_macos.sh uninstall
```

Logs are written under:

```text
~/Library/Logs/Polymarket/portfolio-supervisor.*.log
~/Library/Logs/Polymarket/cross-venue.*.log
```

Runtime state is written under:

```text
~/polymarket/runs/supervisor/
~/polymarket/runs/cross_venue/
```

The initial `config/cross_venue_pairs.csv` has no active verified pair. This is deliberate: market discovery, candidate matching, authentication, telemetry and health run immediately, but hard-arbitrage paper entries remain disabled until a pair's settlement rules are reviewed and `settlement_verified=true` is set explicitly.
