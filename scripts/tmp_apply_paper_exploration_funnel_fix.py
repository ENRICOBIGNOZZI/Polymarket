#!/usr/bin/env python3
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "scripts/v7_external_fair_paper_router.py"
CONFIG = ROOT / "config/v7_external_fair.json"
DIAG = ROOT / "diagnostics/paper_exploration_root_cause.json"


def insert_after_line(lines: list[str], line_no_1: int, payload: list[str]) -> None:
    lines[line_no_1:line_no_1] = payload


def insert_before_line(lines: list[str], line_no_1: int, payload: list[str]) -> None:
    lines[line_no_1 - 1:line_no_1 - 1] = payload


def class_and_method(tree: ast.AST, method: str) -> tuple[ast.ClassDef, ast.FunctionDef]:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == method:
                    return node, child
    raise RuntimeError(f"method_not_found:{method}")


def assignment_call(method: ast.FunctionDef, target_name: str, attr_name: str) -> ast.Assign:
    for node in ast.walk(method):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id != target_name:
            continue
        call = node.value
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == attr_name:
            return node
    raise RuntimeError(f"assignment_not_found:{target_name}:{attr_name}")


def patch_config() -> None:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    pe = cfg.get("paper_exploration")
    if not isinstance(pe, dict):
        raise RuntimeError("paper_exploration_config_missing")
    if pe.get("authority") != "PAPER_EXPLORATION" or pe.get("real_money_authority") is not False:
        raise RuntimeError("paper_exploration_safety_contract_invalid")
    pe["enabled"] = True
    pe["deterministic_coordinator_kick"] = True
    pe["coordinator_receipt_timeout_seconds"] = 3.0
    pe["emit_reject_funnel"] = True
    cfg["paper_exploration"] = pe
    CONFIG.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def patch_router() -> dict[str, object]:
    source = ROUTER.read_text(encoding="utf-8")
    before_gate_lines = [
        {"line": i + 1, "text": line.strip()}
        for i, line in enumerate(source.splitlines())
        if "enabled_for_execution" in line or "PAPER_EXPLORATION" in line or "wait_for_exploration_receipt" in line
    ]
    tree = ast.parse(source)
    _, init = class_and_method(tree, "__init__")
    _, wait_method = class_and_method(tree, "wait_for_exploration_receipt")
    _, attempt = class_and_method(tree, "attempt")
    lines = source.splitlines()

    if "self.paper_exploration_enabled" not in source:
        payload = [
            "        _paper_cfg = load(Path(__file__).resolve().parents[1] / \"config/v7_external_fair.json\")",
            "        _paper_exploration = _paper_cfg.get(\"paper_exploration\") if isinstance(_paper_cfg.get(\"paper_exploration\"), dict) else {}",
            "        self.paper_exploration_enabled = bool(",
            "            _paper_exploration.get(\"enabled\") is True",
            "            and _paper_exploration.get(\"authority\") == \"PAPER_EXPLORATION\"",
            "            and _paper_exploration.get(\"real_money_authority\") is False",
            "            and _paper_exploration.get(\"promotion_credit\") is False",
            "        )",
            "        self.paper_exploration_receipt_timeout_seconds = max(",
            "            0.25, min(10.0, float(_paper_exploration.get(\"coordinator_receipt_timeout_seconds\") or 3.0))",
            "        )",
            "        self.last_coordinator_kick_error: str | None = None",
        ]
        insert_after_line(lines, init.end_lineno or init.lineno, payload)
        source = "\n".join(lines) + "\n"
        tree = ast.parse(source)
        _, wait_method = class_and_method(tree, "wait_for_exploration_receipt")
        _, attempt = class_and_method(tree, "attempt")
        lines = source.splitlines()

    # Retire the old mature-execution kill switch only inside the explicitly checked-in
    # bounded PAPER_EXPLORATION sleeve. Real-money flags remain false everywhere.
    replacements = {
        "if not self.taker_enabled_for_execution:": "if not self.taker_enabled_for_execution and not self.paper_exploration_enabled:",
        "if self.taker_enabled_for_execution is not True:": "if self.taker_enabled_for_execution is not True and not self.paper_exploration_enabled:",
        "if not self.enabled_for_execution:": "if not self.enabled_for_execution and not self.paper_exploration_enabled:",
        "if self.enabled_for_execution is not True:": "if self.enabled_for_execution is not True and not self.paper_exploration_enabled:",
    }
    applied_gate_replacements = 0
    for old, new in replacements.items():
        count = source.count(old)
        if count:
            source = source.replace(old, new)
            applied_gate_replacements += count

    tree = ast.parse(source)
    cls, wait_method = class_and_method(tree, "wait_for_exploration_receipt")
    _, attempt = class_and_method(tree, "attempt")
    lines = source.splitlines()

    if "def kick_global_coordinator" not in source:
        payload = [
            "    def write_paper_exploration_funnel(self, stage: str, **details: object) -> None:",
            "        atomic_json(self.root / \"external_fair\" / \"paper_exploration_funnel.json\", {",
            "            \"schema\": \"polymarket_v7_paper_exploration_funnel_v1\",",
            "            \"timestamp_ns\": time.time_ns(),",
            "            \"model_sha\": self.sha,",
            "            \"stage\": stage,",
            "            \"paper_exploration_enabled\": self.paper_exploration_enabled,",
            "            \"taker_enabled_for_execution\": bool(getattr(self, \"taker_enabled_for_execution\", False)),",
            "            \"last_attempt_reason\": self.last_attempt_reason,",
            "            \"last_coordinator_kick_error\": self.last_coordinator_kick_error,",
            "            \"paper_only\": True,",
            "            \"authenticated_execution\": False,",
            "            \"real_order_submission\": False,",
            "            \"real_capital_at_risk\": False,",
            "            \"details\": details,",
            "        })",
            "",
            "    def kick_global_coordinator(self) -> bool:",
            "        if not self.paper_exploration_enabled:",
            "            return False",
            "        try:",
            "            from v7_global_portfolio_coordinator import process_cut",
            "            process_cut(self.root, now_ns=time.time_ns())",
            "            self.last_coordinator_kick_error = None",
            "            return True",
            "        except Exception as exc:",
            "            self.last_coordinator_kick_error = f\"{type(exc).__name__}:{exc}\"[:300]",
            "            self.write_paper_exploration_funnel(\"COORDINATOR_KICK_FAILED\")",
            "            return False",
            "",
        ]
        insert_before_line(lines, wait_method.lineno, payload)
        source = "\n".join(lines) + "\n"

    # Use the checked-in bounded timeout rather than a hard-coded one second race.
    source = re.sub(
        r"def wait_for_exploration_receipt\(self, replay_key: str, timeout_seconds: float = 1\.0\)",
        "def wait_for_exploration_receipt(self, replay_key: str, timeout_seconds: float | None = None)",
        source,
        count=1,
    )
    tree = ast.parse(source)
    _, wait_method = class_and_method(tree, "wait_for_exploration_receipt")
    lines = source.splitlines()
    method_text = "\n".join(lines[wait_method.lineno - 1:(wait_method.end_lineno or wait_method.lineno)])
    if "paper_exploration_receipt_timeout_seconds" not in method_text:
        insert_after_line(lines, wait_method.lineno, [
            "        if timeout_seconds is None:",
            "            timeout_seconds = self.paper_exploration_receipt_timeout_seconds",
        ])
        source = "\n".join(lines) + "\n"

    # The old code emitted a candidate and synchronously waited while the canonical
    # coordinator had not necessarily run yet. Execute the canonical process_cut
    # deterministically, then consume its receipt. No parallel OMS is introduced.
    tree = ast.parse(source)
    _, attempt = class_and_method(tree, "attempt")
    assign = assignment_call(attempt, "replay_key", "emit_shadow_ingress")
    lines = source.splitlines()
    nearby = "\n".join(lines[(assign.end_lineno or assign.lineno):min(len(lines), (assign.end_lineno or assign.lineno) + 8)])
    if "kick_global_coordinator" not in nearby:
        insert_after_line(lines, assign.end_lineno or assign.lineno, [
            "        if replay_key:",
            "            self.write_paper_exploration_funnel(\"CANDIDATE_EMITTED\", replay_key=replay_key)",
            "            self.kick_global_coordinator()",
        ])
        source = "\n".join(lines) + "\n"

    # Make receipt success and timeout observable without changing the economic gate.
    source = source.replace(
        "        if receipt is None:\n            self.emit_counterfactual(",
        "        if receipt is None:\n            self.write_paper_exploration_funnel(\"RECEIPT_NOT_GRANTED\", replay_key=str(replay_key or \"\"))\n            self.emit_counterfactual(",
        1,
    )
    receipt_anchor = "        canonical_metadata = {\n"
    if receipt_anchor in source and "RECEIPT_GRANTED" not in source:
        source = source.replace(
            receipt_anchor,
            "        self.write_paper_exploration_funnel(\"RECEIPT_GRANTED\", replay_key=str(replay_key or \"\"))\n" + receipt_anchor,
            1,
        )

    ROUTER.write_text(source, encoding="utf-8")
    ast.parse(source)
    after_gate_lines = [
        {"line": i + 1, "text": line.strip()}
        for i, line in enumerate(source.splitlines())
        if "enabled_for_execution" in line or "PAPER_EXPLORATION" in line or "wait_for_exploration_receipt" in line or "kick_global_coordinator" in line
    ]
    return {
        "applied_gate_replacements": applied_gate_replacements,
        "before_gate_lines": before_gate_lines,
        "after_gate_lines": after_gate_lines,
    }


def add_test() -> None:
    test = ROOT / "tests/test_v7_paper_exploration_funnel_fix.py"
    test.write_text('''from __future__ import annotations
import ast, json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_checked_in_exploration_remains_non_real_money():
    cfg=json.loads((ROOT/"config/v7_external_fair.json").read_text())
    pe=cfg["paper_exploration"]
    assert pe["enabled"] is True
    assert pe["authority"]=="PAPER_EXPLORATION"
    assert pe["real_money_authority"] is False
    assert pe["promotion_credit"] is False
    assert pe["deterministic_coordinator_kick"] is True

def test_candidate_is_coordinated_before_receipt_wait():
    text=(ROOT/"scripts/v7_external_fair_paper_router.py").read_text()
    tree=ast.parse(text)
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and any(isinstance(x,ast.FunctionDef) and x.name=="attempt" for x in n.body))
    method=next(x for x in cls.body if isinstance(x,ast.FunctionDef) and x.name=="attempt")
    segment="\\n".join(text.splitlines()[method.lineno-1:method.end_lineno])
    emit=segment.index("replay_key = self.emit_shadow_ingress")
    kick=segment.index("self.kick_global_coordinator()",emit)
    wait=segment.index("self.wait_for_exploration_receipt",kick)
    assert emit < kick < wait

def test_no_plain_mature_execution_kill_switch_survives():
    text=(ROOT/"scripts/v7_external_fair_paper_router.py").read_text()
    assert "if not self.taker_enabled_for_execution:" not in text
    assert "if self.taker_enabled_for_execution is not True:" not in text
    assert "def kick_global_coordinator" in text
    assert "paper_exploration_funnel.json" in text
''', encoding="utf-8")


def main() -> None:
    patch_config()
    report = patch_router()
    add_test()
    diagnosis = json.loads(DIAG.read_text(encoding="utf-8")) if DIAG.exists() else {}
    output = {
        "schema": "polymarket_v7_paper_exploration_funnel_repair_v1",
        "diagnosed_causes": diagnosis.get("causes", []),
        "repair": report,
        "safety": {
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "real_capital_at_risk": False,
        },
    }
    (ROOT / "diagnostics/paper_exploration_repair.json").write_text(
        json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
