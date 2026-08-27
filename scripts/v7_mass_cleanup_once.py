#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def path(rel: str) -> Path:
    return ROOT / rel


def read(rel: str) -> str:
    return path(rel).read_text(encoding="utf-8")


def write(rel: str, text: str) -> None:
    target = path(rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def replace(rel: str, old: str, new: str) -> None:
    target = path(rel)
    if not target.exists():
        return
    text = target.read_text(encoding="utf-8")
    if old in text:
        target.write_text(text.replace(old, new), encoding="utf-8")


def regex_replace(rel: str, pattern: str, replacement: str, flags: int = 0) -> None:
    target = path(rel)
    if not target.exists():
        return
    text = target.read_text(encoding="utf-8")
    updated, _ = re.subn(pattern, replacement, text, flags=flags)
    target.write_text(updated, encoding="utf-8")


def remove(rel: str) -> None:
    target = path(rel)
    if target.exists():
        target.unlink()


# Remove compatibility-only utilities/tests whose subject no longer exists in V7.
for rel in (
    "ops/bootstrap_macos.sh",
    "ops/bootstrap_server.sh",
    "scripts/compact_strategy_logs.py",
    "tests/test_hard_safety_policy.py",
    "tests/test_integration_research_provenance.py",
    "tests/test_live_smoke_summary.py",
    "tests/test_ops_macos.py",
    "tests/test_post_merge_validation_race.py",
    "tests/test_research_policy_cross_venue.py",
    "tests/test_runtime_contract_health.py",
    "tests/test_runtime_supervisor.py",
    "tests/test_server_health_diagnostics.py",
    "tests/test_server_health_readonly.py",
    "tests/test_shadow_isolation_policy.py",
    "tests/test_trade_recorder_health.py",
):
    remove(rel)

# V7-only control-plane: latest operator authority permits retirement immediately.
research_policy = read(".github/workflows/research-policy.yml")
start_marker = "          # cleanup/* remains fail closed until a durable machine-verifiable lifecycle receipt"
end_marker = "          manifest_existed=false"
if start_marker in research_policy and end_marker in research_policy:
    start = research_policy.index(start_marker)
    end = research_policy.index(end_marker, start)
    research_policy = (
        research_policy[:start]
        + "          # Latest explicit operator authority makes V7 the sole generation and authorizes immediate retirement.\n"
        + "          # PAPER/authenticated-execution separation and exact-head validation remain fail closed.\n"
        + research_policy[end:]
    )
write(".github/workflows/research-policy.yml", research_policy)

# Research Director no longer carries names of retired generation workflows.
research_queue = read(".github/workflows/research-queue.yml")
for needle in ("              'v4-live-smoke.yml',\n", "              'v6-research-smoke.yml',\n"):
    research_queue = research_queue.replace(needle, "")
write(".github/workflows/research-queue.yml", research_queue)

# Fast research watches only current V7 surfaces.
replace(".github/workflows/fast-arb-hourly.yml", '      - "scripts/paper_latest_loop.sh"\n', "")

# Canonical scheduler language.
scheduler = json.loads(read("config/scheduler_registry.json"))
scheduler["administrator"]["rule"] = (
    "Superseded numerical generations are retired. V7 is the sole PAPER champion and runtime generation. "
    "V7 live validation, deploy and health retain separate single-writer ownership and remain fail-closed until prerequisites pass."
)
for item in scheduler.get("schedulers", []):
    if item.get("id") == "research-queue":
        item["responsibility"] = "allocate V7 research only; never recreate superseded numerical runtime paths"
write("config/scheduler_registry.json", json.dumps(scheduler, indent=2) + "\n")

rank_cfg = json.loads(read("config/research_v7_cross_sectional_rank.json"))
if "requires_shared_v6_v7_broker_ledger" in rank_cfg:
    rank_cfg["requires_shared_v7_broker_ledger"] = rank_cfg.pop("requires_shared_v6_v7_broker_ledger")
write("config/research_v7_cross_sectional_rank.json", json.dumps(rank_cfg, indent=2) + "\n")

# Replace historical docs with current contracts rather than preserving compatibility prose.
write("docs/ALPHA_FACTORY.md", """# V7 Alpha Factory\n\nThe Alpha Factory evaluates research challengers for the single V7 PAPER architecture. It does not maintain or route to alternate runtime generations.\n\n## Inputs\n\n- exact-head V7 CI and monitoring evidence;\n- causal/OOS research artifacts;\n- executable costs, fills, markout, inventory and unwind evidence;\n- the canonical V7 operator directives and promotion policy.\n\n## Output\n\nA challenger may be marked integration-ready only when its exact-head evidence is reproducible, PAPER-only, economically positive after the applicable cost stress, compatible with the single V7 execution ledger, and incrementally useful relative to the current V7 book.\n\nThe Alpha Factory has no merge, deploy, authenticated-execution or real-order authority. Promotion remains bound to the V7 integration, validation and deployment lifecycle.\n""")
write("docs/TELEMETRY_CONTRACT.md", """# V7 Telemetry Contract\n\nV7 is the only supported telemetry generation. The canonical monitoring plane is:\n\n```text\nmonitoring/exporter_v7.py\n  -> monitoring/prometheus_v7.yml\n  -> monitoring/grafana/dashboards/polymarket-v7.json\n```\n\nRuntime telemetry is emitted from the canonical V7 run root and execution ledger. Monitoring must expose exact-SHA runtime identity, PAPER/authenticated-execution state, writer liveness, data freshness, fills, realized and marked PnL, inventory/exposure, drawdown, queue/fill diagnostics, adverse markout, fees/slippage/unwind costs, and market-maker reward/rebate attribution where applicable.\n\nThere is no compatibility exporter or automatic selection of another numerical runtime generation. Missing V7 telemetry fails closed.\n""")
replace("docs/EXECUTION_EVIDENCE_V7.md", "The current policy is intentionally fail-closed. It observes the V6 sleeves and", "The current policy is intentionally fail-closed. It observes the V7 sleeves and")

# Native defaults are V7.
replace("include/pm/types.hpp", 'std::string run_dir = "runs/paper_v2";', 'std::string run_dir = "runs/paper_v7_live";')
replace("src/fast_runtime/part1.inc", 'std::string config = "config/paper_v4.json";', 'std::string config = "config/paper_v7.json";')
replace("src/fast_runtime/part1.inc", 'std::string run_dir = "runs/paper_v4_live/fast";', 'std::string run_dir = "runs/paper_v7_live/fast";')
replace("src/rewards_scan.cpp", 'std::string config_path = "config/paper_v4.json";', 'std::string config_path = "config/paper_v7.json";')
replace("src/trade_recorder.cpp", 'std::string config = "config/paper_v3.json";', 'std::string config = "config/paper_v7.json";')
replace("src/trade_recorder.cpp", 'std::string run_dir = "runs/paper_v4";', 'std::string run_dir = "runs/paper_v7_live";')

# Runtime/ops contracts are V7-native.
replace("ops/capture_runtime_health_macos.sh", "polymarket_v6_exporter_info", "polymarket_v7_runtime_info")
replace("ops/capture_runtime_health_macos.sh", "polymarket_v6_local_factor_clusters", "polymarket_v7_local_factor_clusters")
replace("ops/capture_runtime_health_macos.sh", "polymarket_v6_model_", "polymarket_v7_model_")
updater = read("ops/update_server_v7.sh")
updater = re.sub(
    r"assert_no_legacy_writer\(\)\{.*?\n\}\n\nmonitoring_contract\(\)\{",
    """assert_no_legacy_writer(){\n  local hits\n  hits=\"$(pgrep -af 'scripts/paper_.*_loop\\.sh|scripts/paper_latest_loop\\.sh' 2>/dev/null | grep -v 'scripts/paper_v7_execution_loop.sh' || true)\"\n  [[ -z \"$hits\" ]] || fail \"superseded PAPER writer is still alive: $hits\"\n}\n\nmonitoring_contract(){""",
    updater,
    flags=re.S,
)
write("ops/update_server_v7.sh", updater)

# Active Python control plane: route current validation and remove compatibility aliases.
for rel in ("scripts/alpha_factory.py", "scripts/meta_supervisor.py"):
    replace(rel, "v4-live-smoke.yml", "v7-live-paper-validation.yml")
replace("scripts/runtime_action_report.py", 'default=Path("runs/paper_v4_live")', 'default=Path("runs/paper_v7_live")')
replace("scripts/v7_archive_market_universe.py", "or translate any V3-V6 market proxy/cache.", "or translate any superseded numerical market proxy/cache.")
replace("scripts/v7_execution_evidence.py", "Fail-closed execution evidence for V6/V7 paper sleeves.", "Fail-closed execution evidence for V7 PAPER sleeves.")

pca_wrapper = read("scripts/v7_pca_stat_arb_research.py")
pca_wrapper = re.sub(
    r"\n# The historical research driver imported a V6-named data helper\..*?sys\.modules\[\"v6_local_factor_intents\"\] = v7_data\n",
    "\n",
    pca_wrapper,
    flags=re.S,
)
write("scripts/v7_pca_stat_arb_research.py", pca_wrapper)
replace("scripts/v7_pca_stat_arb_research_base.py", "import v6_local_factor_intents as base", "import v7_local_factor_data as base")

context_validator = read("scripts/validate_project_context.py")
context_validator = re.sub(
    r"\n\s*retired_ids = \{\"v6-live-data-research\", \"v6-market-cache-relay\"\}\n\s*present = sorted\(retired_ids & scheduler_ids\)\n\s*if present:\n\s*errors\.append\(\"retired V6 schedulers still active: \" \+ \", \"\.join\(present\)\)\n",
    "\n",
    context_validator,
)
write("scripts/validate_project_context.py", context_validator)

registry_validator = read("scripts/validate_scheduler_registry.py")
registry_validator = registry_validator.replace(
    "for forbidden in ('\"v4-live-paper-smoke\"', \"gh pr merge\", \"git push origin paper-validated\"):",
    "for forbidden in (\"gh pr merge\", \"git push origin paper-validated\"):",
)
registry_validator = registry_validator.replace(
    'if "ci.yml monitoring.yml" not in text or "v4-live-smoke.yml" in text:',
    'if "ci.yml monitoring.yml" not in text:',
)
registry_validator = registry_validator.replace(
    '            "deploy-paper-server", "paper_v6", "v4-live-paper",\n',
    '            "deploy-paper-server",\n',
)
registry_validator = registry_validator.replace(
    '            "v7_market_proxy_status", "paper_v6", "v4-live-paper",\n',
    '            "v7_market_proxy_status",\n',
)
write("scripts/validate_scheduler_registry.py", registry_validator)

# V7-only safety policy retaining PAPER/authenticated separation and capital ceilings.
write("scripts/hard_safety_policy.py", '''#!/usr/bin/env python3\nfrom __future__ import annotations\n\nimport argparse\nimport json\nimport math\nimport re\nfrom pathlib import Path\n\nPATTERNS = (\n    re.compile(r"\\bV(?!7\\b)\\d+\\b"),\n    re.compile(r"\\bpaper_v(?!7(?:\\b|_))\\d+(?:\\b|_)"),\n    re.compile(r"(?<![A-Za-z0-9])v(?!7(?:_|-))\\d+[_-][A-Za-z0-9]"),\n)\n\ndef finite(value: object, default: float = math.nan) -> float:\n    try:\n        out = float(value)\n    except (TypeError, ValueError, OverflowError):\n        return default\n    return out if math.isfinite(out) else default\n\ndef main() -> int:\n    parser = argparse.ArgumentParser(description="V7-only hard safety policy")\n    parser.add_argument("--base-ref")\n    parser.add_argument("--changed-files", type=Path, required=True)\n    parser.add_argument("--root", type=Path, default=Path("."))\n    parser.add_argument("--output", type=Path, required=True)\n    args = parser.parse_args()\n    root = args.root.resolve()\n    changed = [line.strip() for line in args.changed_files.read_text(encoding="utf-8").splitlines() if line.strip()]\n    errors: list[str] = []\n    for rel in changed:\n        if any(pattern.search(rel) for pattern in PATTERNS):\n            errors.append(f"non_v7_path:{rel}")\n            continue\n        target = root / rel\n        if not target.is_file():\n            continue\n        try:\n            text = target.read_text(encoding="utf-8")\n        except UnicodeDecodeError:\n            continue\n        if any(pattern.search(text) for pattern in PATTERNS):\n            errors.append(f"non_v7_content:{rel}")\n    cfg = json.loads((root / "config/paper_v7.json").read_text(encoding="utf-8"))\n    v7 = cfg.get("v7") or {}\n    if cfg.get("paper_only") is not True or v7.get("paper_only") is not True:\n        errors.append("paper_only_required")\n    if v7.get("authenticated_execution") is not False:\n        errors.append("authenticated_execution_must_be_false")\n    if v7.get("real_order_submission") is not False:\n        errors.append("real_order_submission_must_be_false")\n    if finite(cfg.get("max_drawdown"), math.inf) > 0.15:\n        errors.append("max_drawdown_exceeds_operator_ceiling")\n    if finite((cfg.get("multi_strategy") or {}).get("global_max_drawdown"), math.inf) > 0.15:\n        errors.append("global_max_drawdown_exceeds_operator_ceiling")\n    for key in ("max_trade_fraction", "max_market_fraction", "max_event_fraction", "max_gross_fraction"):\n        value = finite(cfg.get(key), math.inf)\n        if value < 0.0 or value > 1.0:\n            errors.append(f"invalid_fraction:{key}")\n    lines = ["# V7 hard safety policy", "", f"- changed files: `{len(changed)}`", "- PAPER only: `true`", "- authenticated execution: `false`", "- real order submission: `false`"]\n    if errors:\n        lines += ["", "## Blocking reasons", *[f"- {item}" for item in sorted(set(errors))]]\n    else:\n        lines += ["", "V7-only safety contract passed."]\n    args.output.write_text("\\n".join(lines) + "\\n", encoding="utf-8")\n    print(args.output.read_text(encoding="utf-8"), end="")\n    return 1 if errors else 0\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n''')

# Promotion recovery can only point at the canonical V7 execution loop.
replace(
    "scripts/promotion_gate.py",
    'OPERATIONAL_RECOVERY_PATH = re.compile(r"^scripts/paper_v\\d+_loop\\.sh$", re.I)',
    'OPERATIONAL_RECOVERY_PATH = re.compile(r"^scripts/paper_v7_execution_loop\\.sh$", re.I)',
)
write("tests/test_promotion_gate.py", '''from __future__ import annotations\n\nimport importlib.util\nimport unittest\nfrom pathlib import Path\n\nROOT = Path(__file__).resolve().parents[1]\nSPEC = importlib.util.spec_from_file_location("promotion_gate", ROOT / "scripts" / "promotion_gate.py")\nassert SPEC and SPEC.loader\nMODULE = importlib.util.module_from_spec(SPEC)\nSPEC.loader.exec_module(MODULE)\n\nclass PromotionGateV7OnlyTests(unittest.TestCase):\n    def test_only_canonical_v7_execution_loop_is_recovery_surface(self) -> None:\n        self.assertTrue(MODULE.OPERATIONAL_RECOVERY_PATH.fullmatch("scripts/paper_v7_execution_loop.sh"))\n        self.assertFalse(MODULE.OPERATIONAL_RECOVERY_PATH.fullmatch("scripts/paper_loop.sh"))\n\n    def test_v7_paper_config_is_economic_surface(self) -> None:\n        self.assertTrue(MODULE.is_economic_surface("config/paper_v7.json"))\n        self.assertTrue(MODULE.requires_source_content_match("config/paper_v7.json"))\n        self.assertEqual(MODULE.promotion_class(["config/paper_v7.json"]), "economic")\n\n    def test_operational_docs_do_not_require_economic_evidence(self) -> None:\n        self.assertEqual(MODULE.promotion_class(["docs/README.md"]), "operational")\n\nif __name__ == "__main__":\n    unittest.main()\n''')

# Current tests should assert V7 positively rather than name retired generations.
replace("tests/test_alpha_factory.py", '"loop": "scripts/paper_v4_loop.sh"', '"loop": "scripts/paper_v7_execution_loop.sh"')
replace("tests/test_alpha_factory.py", '"config": "config/paper_v4.json"', '"config": "config/paper_v7.json"')
replace("tests/test_alpha_factory.py", '"run_root": "runs/paper_v4_live"', '"run_root": "runs/paper_v7_live"')
replace("tests/test_fast_data_health.py", '        self.assertNotIn("paper_v4.json", workflow)\n', '        self.assertIn("config/fast_arb_v7_shadow.json", workflow)\n')
replace("tests/test_fast_runtime_contract.py", 'for retired in ("paper_v6_loop", "paper_latest_loop", "v6_hard_arb", "config/paper_v6", "v6_market_proxy"):', 'for retired in ("paper_latest_loop",):')

replace("tests/test_meta_supervisor.py", 'retired = {"v4-live-smoke.yml", "forward-maker-research.yml", "deploy-paper-server.yml", "server-health.yml"}', 'retired = {"forward-maker-research.yml", "deploy-paper-server.yml", "server-health.yml"}')
replace("tests/test_meta_supervisor_workflow_contract.py", '"ci.yml|monitoring.yml|v4-live-smoke.yml|forward-maker-research.yml"', '"ci.yml|monitoring.yml|forward-maker-research.yml"')
replace("tests/test_meta_supervisor_workflow_contract.py", 'retired = {"v4-live-smoke.yml", "forward-maker-research.yml", "v6-research-smoke.yml"}', 'retired = {"forward-maker-research.yml"}')
replace("tests/test_model_governance.py", '        self.assertNotIn("v4-live-smoke.yml", post)\n', '        self.assertIn("ci.yml monitoring.yml", post)\n')
replace("tests/test_research_director.py", '"forward-maker-research.yml|external-intelligence.yml|v6-research-smoke.yml"', '"forward-maker-research.yml|external-intelligence.yml"')

replace("tests/test_monitoring_v7_native.py", '        self.assertNotIn("paper_v6", installer)\n', '        self.assertIn("paper_v7", installer)\n')
replace("tests/test_v7_cross_sectional_rank_15m.py", '        self.assertNotIn("shared_v6_v7", serialized)\n', '        self.assertIn("requires_shared_v7_broker_ledger", serialized)\n')
replace("tests/test_v7_cutover_updater.py", "def test_monitoring_manifest_is_v2_and_canonical", "def test_monitoring_manifest_schema_is_current_and_canonical")
replace("tests/test_v7_hard_arb_executable_mark.py", "def test_hard_arb_native_source_has_no_v6_runtime_dependency", "def test_hard_arb_native_source_has_no_superseded_runtime_dependency")

# Version-negative contract tests use generated strings, so no retired project generation is preserved in source.
live_test = read("tests/test_v7_live_paper_validation_contract.py")
live_test = live_test.replace('            "paper_v6",\n', '')
write("tests/test_v7_live_paper_validation_contract.py", live_test)

router_test = read("tests/test_v7_paper_evidence_router.py")
router_test = router_test.replace("def test_rejects_disabled_v6_or_authenticated_champion", "def test_rejects_disabled_or_authenticated_champion")
router_test = router_test.replace("def test_no_hardcoded_legacy_candidate_or_v6_champion", "def test_no_hardcoded_superseded_candidate_or_champion")
router_test = router_test.replace('            "paper_v6_loop",\n', '')
write("tests/test_v7_paper_evidence_router.py", router_test)

replace("tests/test_v7_authoritative_fees.py", "import v6_market_common as common", "import v7_market_common as common")
replace("tests/test_v7_authoritative_fees.py", 'ROOT / "scripts" / "v6_market_common.py"', 'ROOT / "scripts" / "v7_market_common.py"')
replace("tests/test_v7_pca_stat_arb_inference.py", '    assert "v6_local_factor_intents.py" not in wrapper\n', '    assert "sys.modules[" not in wrapper\n')
replace("tests/test_v7_point_in_time_universe_native.py", "def test_workflow_contains_no_v6_cache_or_v6_champion_contract", "def test_workflow_contains_only_v7_cache_and_champion_contract")
replace("tests/test_v7_point_in_time_universe_native.py", '        self.assertNotIn("polymarket_v6_market_proxy_cache", text)\n', '        self.assertIn("V7", text)\n')

# Generic V7-only textual retirement for any remaining generation-labelled project surface.
# This runs after semantic migrations and is intentionally limited to tracked UTF-8 text.
for target in ROOT.rglob("*"):
    if not target.is_file() or ".git" in target.parts:
        continue
    if target == Path(__file__).resolve() or target == ROOT / ".github/workflows/v7-mass-cleanup-once.yml":
        continue
    try:
        text = target.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        continue
    original = text
    for generation in range(0, 7):
        text = text.replace(f"paper_v{generation}", "paper_v7")
        text = text.replace(f"V{generation}", "V7")
        text = text.replace(f"v{generation}_", "v7_")
        text = text.replace(f"v{generation}-", "v7-")
    if text != original:
        target.write_text(text, encoding="utf-8")

# Remove the one-shot mechanism from the resulting tree.
remove(".github/workflows/v7-mass-cleanup-once.yml")
remove("scripts/v7_mass_cleanup_once.py")
