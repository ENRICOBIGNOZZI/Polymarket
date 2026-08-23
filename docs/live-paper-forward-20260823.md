# Live Paper Forward Test — 2026-08-23

Temporary experiment branch used to run a short live-data paper-trading forward test through GitHub Actions. The job executes 20 one-shot paper snapshots against public Gamma/CLOB data, preserves the same run directory between snapshots, and uploads the runtime logs as an artifact. No authenticated order submission exists or is invoked.
