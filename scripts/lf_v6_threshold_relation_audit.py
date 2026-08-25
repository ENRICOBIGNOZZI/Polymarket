#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import math
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "v6_relation_intents.py"

MONTH = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
FULL_DATE = re.compile(rf"\b(?P<month>{MONTH})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<year>20\d{{2}}))?\b", re.I)
YEAR = re.compile(r"\b20\d{2}\b")
UP = re.compile(r"\b(?:above|over|exceed(?:s)?|reach(?:es)?|at least|more than|higher than|or more)\b", re.I)
DOWN = re.compile(r"\b(?:below|under|dip to|fall to|at most|less than|lower than|or fewer|or less)\b", re.I)
NUMBER = re.compile(r"(?P<prefix>[$€£]?)\s*(?P<num>\d[\d,]*(?:\.\d+)?)(?P<suffix>bps|bp|k|m|b|%)?(?![A-Za-z])", re.I)


@dataclass(frozen=True)
class Signature:
    family: str
    direction: str
    threshold: float
    kind: str
    expiry: str


def load_v6_module():
    spec = importlib.util.spec_from_file_location("v6_relation_intents", SOURCE)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load V6 relation source")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def numeric_value(match: re.Match[str]) -> tuple[float, str]:
    raw = float(match.group("num").replace(",", ""))
    prefix = match.group("prefix") or ""
    suffix = (match.group("suffix") or "").lower()
    if suffix == "k":
        raw *= 1e3
    elif suffix == "m":
        raw *= 1e6
    elif suffix == "b":
        raw *= 1e9
    elif suffix == "%":
        raw /= 100.0
    elif suffix in {"bp", "bps"}:
        raw /= 10000.0
    if prefix:
        kind = "money"
    elif suffix == "%":
        kind = "percent"
    elif suffix in {"bp", "bps"}:
        kind = "bps"
    elif suffix in {"k", "m", "b"}:
        kind = "scaled_count"
    else:
        kind = "count"
    return raw, kind


def expiry_signature(question: str) -> str:
    dates = []
    for match in FULL_DATE.finditer(question):
        month = match.group("month").lower()[:3]
        day = int(match.group("day"))
        year = match.group("year") or ""
        dates.append(f"{year or 'xxxx'}-{month}-{day:02d}")
    if dates:
        return ";".join(dates)
    years = YEAR.findall(question)
    return ";".join(years)


def typed_threshold_signature(question: str) -> Signature | None:
    up = UP.search(question)
    down = DOWN.search(question)
    if up and down:
        return None
    direction_match = up or down
    if direction_match is None:
        return None
    direction = "UP" if up else "DOWN"

    candidates: list[tuple[tuple[int, int, float], re.Match[str], float, str]] = []
    for match in NUMBER.finditer(question):
        value, kind = numeric_value(match)
        plain_year = kind == "count" and 1900 <= value <= 2100 and match.group("num").isdigit() and len(match.group("num")) == 4
        if plain_year:
            continue
        explicit = 2 if kind in {"money", "percent", "bps", "scaled_count"} else 1
        distance = min(abs(match.start() - direction_match.end()), abs(direction_match.start() - match.end()))
        candidates.append(((explicit, -distance, value), match, value, kind))
    if not candidates:
        return None
    _, chosen, threshold, kind = max(candidates, key=lambda item: item[0])

    text = question.lower()
    a, b = chosen.span()
    text = text[:a] + f" <threshold:{kind}> " + text[b:]
    text = UP.sub(" <direction> ", text)
    text = DOWN.sub(" <direction> ", text)
    text = FULL_DATE.sub(" <expiry> ", text)
    text = YEAR.sub(" <year> ", text)
    text = re.sub(r"[^a-z<>:%]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    expiry = expiry_signature(question)
    return Signature(f"{text}|expiry={expiry}|kind={kind}", direction, threshold, kind, expiry)


def fixture_report() -> dict[str, Any]:
    v6 = load_v6_module()
    cases = {
        "cross_expiry_a": "Will the price of Bitcoin be above $100,000 on August 25, 2026?",
        "cross_expiry_b": "Will the price of Bitcoin be above $150,000 on August 31, 2026?",
        "suffix_bleed": "Will Bitcoin be above $100,000 by December 31, 2026?",
        "count_four": "Will the Fed make at least 4 rate cuts in 2026?",
        "count_five": "Will the Fed make at least 5 rate cuts in 2026?",
        "or_more": "Will 2 or more hurricanes make landfall in the US in 2026?",
        "percent": "Will Futuro Nazionale get at least 3% of the vote in the next Italian general elections?",
    }
    incumbent: dict[str, Any] = {}
    challenger: dict[str, Any] = {}
    for name, question in cases.items():
        old = v6.threshold_signature(question)
        incumbent[name] = None if old is None else {"family": old[0], "direction": old[1], "threshold": old[2]}
        new = typed_threshold_signature(question)
        challenger[name] = None if new is None else asdict(new)

    cross_old_same_family = incumbent["cross_expiry_a"] is not None and incumbent["cross_expiry_b"] is not None and incumbent["cross_expiry_a"]["family"] == incumbent["cross_expiry_b"]["family"]
    cross_new_same_family = challenger["cross_expiry_a"] is not None and challenger["cross_expiry_b"] is not None and challenger["cross_expiry_a"]["family"] == challenger["cross_expiry_b"]["family"]
    return {
        "source": str(SOURCE),
        "decision": "MORE_EVIDENCE_REQUIRED",
        "incumbent": incumbent,
        "typed_challenger": challenger,
        "findings": {
            "incumbent_merges_different_day_expiries": cross_old_same_family,
            "typed_challenger_merges_different_day_expiries": cross_new_same_family,
            "incumbent_count_thresholds_choose_year": incumbent["count_four"]["threshold"] == 2026.0 and incumbent["count_five"]["threshold"] == 2026.0,
            "incumbent_or_more_is_unrecognized": incumbent["or_more"] is None,
            "incumbent_suffix_bleeds_into_by": incumbent["suffix_bleed"] is not None and incumbent["suffix_bleed"]["threshold"] > 1e10,
            "typed_count_thresholds_are_distinct": challenger["count_four"]["threshold"] == 4.0 and challenger["count_five"]["threshold"] == 5.0,
            "typed_or_more_is_recognized": challenger["or_more"] is not None and challenger["or_more"]["threshold"] == 2.0,
            "typed_money_by_is_100k": challenger["suffix_bleed"] is not None and math.isclose(challenger["suffix_bleed"]["threshold"], 100000.0),
            "typed_percent_is_preserved": challenger["percent"] is not None and challenger["percent"]["kind"] == "percent" and math.isclose(challenger["percent"]["threshold"], 0.03),
        },
    }


def main() -> int:
    print(json.dumps(fixture_report(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
