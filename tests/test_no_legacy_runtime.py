from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()

# V7 is the sole project-generation identifier. Keep these expressions generic
# so the guard itself does not preserve any alternate generation name.
FORBIDDEN_GENERATION_PATTERNS = (
    re.compile(r"\bV(?!7\b)\d+\b"),
    re.compile(r"\bpaper_v(?!7(?:\b|_))\d+(?:\b|_)"),
    re.compile(r"(?<![A-Za-z0-9])v(?!7(?:_|-))\d+[_-][A-Za-z0-9]"),
)

# Build retired compatibility tokens from fragments so this guard does not
# self-match merely by declaring the invariant.
FORBIDDEN_COMPATIBILITY_PATTERNS = (
    re.compile("paper_" + "latest_loop\\.sh"),
    re.compile("multi_" + "strategy_paper\\.py"),
    re.compile("incumbent_" + "health_gate\\.py"),
    re.compile("tiny_" + "live_pilot\\.py"),
    re.compile("filter_" + "coherent_hedges\\.py"),
    re.compile("forward-" + "maker-research\\.yml"),
    re.compile(r"(?<!v7-)deploy-paper-server\.yml"),
    re.compile(r"(?<!paper-)server-health\.yml"),
    re.compile("grafana-" + "access\\.yml"),
    re.compile("exporter_" + "latest\\.py"),
)


def tracked_files() -> list[Path]:
    raw = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    return [ROOT / item.decode("utf-8") for item in raw.split(b"\0") if item]


def scan(patterns: tuple[re.Pattern[str], ...], *, skip_self: bool = False) -> list[str]:
    offenders: list[str] = []
    for path in tracked_files():
        if skip_self and path.resolve() == SELF:
            continue
        relative = path.relative_to(ROOT).as_posix()
        if any(pattern.search(relative) for pattern in patterns):
            offenders.append(relative)
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if any(pattern.search(line) for pattern in patterns):
                offenders.append(f"{relative}:{line_no}: {line.strip()}")
    return sorted(set(offenders))


class NoLegacyRuntimeContractTest(unittest.TestCase):
    def test_v7_is_the_only_project_generation_in_tracked_tree(self) -> None:
        offenders = scan(FORBIDDEN_GENERATION_PATTERNS)
        if offenders:
            print("\nV7-only repository generation offenders:")
            print("\n".join(offenders))
        self.assertEqual(offenders, [], "non-V7 project generation references remain")

    def test_retired_compatibility_names_are_absent_everywhere(self) -> None:
        offenders = scan(FORBIDDEN_COMPATIBILITY_PATTERNS, skip_self=True)
        if offenders:
            print("\nRetired compatibility-name offenders:")
            print("\n".join(offenders))
        self.assertEqual(offenders, [], "retired compatibility references remain")

    def test_known_compatibility_entrypoints_are_absent(self) -> None:
        retired = (
            "scripts/paper_latest_loop.sh",
            "scripts/multi_strategy_paper.py",
            "scripts/incumbent_health_gate.py",
            "scripts/tiny_live_pilot.py",
            "scripts/filter_coherent_hedges.py",
            ".github/workflows/forward-maker-research.yml",
            ".github/workflows/deploy-paper-server.yml",
            ".github/workflows/server-health.yml",
            ".github/workflows/grafana-access.yml",
            "monitoring/exporter.py",
            "monitoring/exporter_latest.py",
            "monitoring/grafana/dashboards/polymarket-fast-paper.json",
            "monitoring/grafana/dashboards/polymarket-multi-strategy.json",
            "src/negrisk_arb.cpp",
            "src/stat_arb.cpp",
            "src/pca_stat_arb.cpp",
            "src/maker_paper.cpp",
            "src/multileg_paper.cpp",
        )
        offenders = [path for path in retired if (ROOT / path).exists()]
        self.assertEqual(offenders, [], "known compatibility entrypoints remain")

    def test_champion_manifest_is_v7_paper_only(self) -> None:
        manifest = json.loads((ROOT / "config/live_champion.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["enabled"])
        self.assertEqual(manifest["version"], 7)
        self.assertTrue(manifest["paper_only"])
        self.assertFalse(manifest["authenticated_execution"])
        self.assertFalse(manifest.get("real_order_submission"))
        self.assertEqual(manifest["loop"], "scripts/paper_v7_execution_loop.sh")
        self.assertEqual(manifest["config"], "config/paper_v7.json")
        self.assertEqual(manifest["run_root"], "runs/paper_v7_live")
        self.assertEqual(manifest["deployment_ref"], "paper-validated")
        self.assertTrue((ROOT / manifest["loop"]).is_file())
        self.assertTrue((ROOT / manifest["config"]).is_file())
        self.assertIn(manifest.get("candidate_only_until_promoted"), (True, False))
        self.assertFalse(manifest.get("legacy_fallback_allowed"))


if __name__ == "__main__":
    unittest.main()
