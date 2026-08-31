"""Independent journal rebuilding API; deliberately does not import the production ledger."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from v7_real_pnl_verifier import verify  # noqa: E402,F401
