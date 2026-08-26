#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

KEY_VALUE = re.compile(r"([A-Za-z_]+)=([^\s]+)")


def _to_number(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _multileg_state(logs: list[str]) -> dict[str, float]:
    state = {"resting": 0.0, "complete": 0.0, "reserved": 0.0, "trades_processed": 0.0}
    for line in logs:
        if not line.startswith("multileg_tick "):
            continue
        fields = dict(KEY_VALUE.findall(line))
        for key in state:
            value = _to_number(fields.get(key, ""))
            if value is not None:
                state[key] = value
    return state


def analyze(snapshot: dict[str, Any], *, smoke_script: str = "", loop_script: str = "") -> dict[str, Any]:
    recorder = snapshot.get("data_health", {}).get("trade_recorder", {})
    recorder_status = str(recorder.get("status", "missing"))
    recorder_failures = [str(x) for x in recorder.get("failures", [])]
    fields = recorder.get("fields", {}) if isinstance(recorder.get("fields", {}), dict) else {}

    intents = snapshot.get("intents", {}) if isinstance(snapshot.get("intents", {}), dict) else {}
    strategies = intents.get("strategies", {}) if isinstance(intents.get("strategies", {}), dict) else {}
    graph_rows = int(strategies.get("GRAPH_RV", 0) or 0)
    graph_bundles = int(intents.get("bundles", 0) or 0) if graph_rows > 0 else 0

    logs = snapshot.get("logs", {}) if isinstance(snapshot.get("logs", {}), dict) else {}
    multileg = _multileg_state([str(x) for x in logs.get("multileg", [])])

    tape_unusable = recorder_status != "healthy"
    graph_execution_active = graph_rows > 0 or multileg["resting"] > 0 or multileg["reserved"] > 0
    unsafe_admission = tape_unusable and graph_execution_active

    smoke_has_gate = "validate_trade_recorder_health.py" in smoke_script if smoke_script else None
    loop_has_gate = "validate_trade_recorder_health.py" in loop_script if loop_script else None

    return {
        "status": "FAIL_CLOSED_REQUIRED" if unsafe_admission else "NO_DEFECT_OBSERVED",
        "unsafe_graph_admission_with_unhealthy_tape": unsafe_admission,
        "trade_recorder": {
            "status": recorder_status,
            "failures": recorder_failures,
            "fetched": int(fields.get("fetched", 0) or 0),
            "new_trades": int(fields.get("new_trades", 0) or 0),
            "last_trade_ts": int(fields.get("last_trade_ts", 0) or 0),
            "seen": int(fields.get("seen", 0) or 0),
        },
        "graph_rv": {
            "intent_rows": graph_rows,
            "intent_bundles": graph_bundles,
            "resting_bundles": int(multileg["resting"]),
            "complete_bundles": int(multileg["complete"]),
            "reserved_usd": multileg["reserved"],
            "trades_processed": int(multileg["trades_processed"]),
        },
        "source_contract": {
            "smoke_invokes_trade_recorder_health_gate": smoke_has_gate,
            "persistent_loop_invokes_trade_recorder_health_gate": loop_has_gate,
        },
        "required_contract": (
            "When the public trade tape is unhealthy or uninitialized, strategies whose queue/joint-completion "
            "economics depend on that tape must not create new executable paper intents or reserve new capital. "
            "Existing state must be handled separately and fail-closed without fabricating fill evidence."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit V6 LF/Graph admission against public trade-tape health")
    parser.add_argument("--telemetry", required=True)
    parser.add_argument("--smoke-script")
    parser.add_argument("--loop-script")
    parser.add_argument("--output")
    args = parser.parse_args()

    snapshot = json.loads(Path(args.telemetry).read_text(encoding="utf-8"))
    smoke = Path(args.smoke_script).read_text(encoding="utf-8") if args.smoke_script else ""
    loop = Path(args.loop_script).read_text(encoding="utf-8") if args.loop_script else ""
    report = analyze(snapshot, smoke_script=smoke, loop_script=loop)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 1 if report["unsafe_graph_admission_with_unhealthy_tape"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
