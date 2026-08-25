#!/usr/bin/env python3
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

try:
    import v6_relation_intents as base
except ModuleNotFoundError:
    from scripts import v6_relation_intents as base

MONTH = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
FULL_DATE = re.compile(rf"\b(?P<month>{MONTH})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<year>20\d{{2}}))?\b", re.I)
YEAR = re.compile(r"\b20\d{2}\b")
UP = re.compile(r"\b(?:above|over|exceed(?:s)?|reach(?:es)?|at least|more than|higher than|or more)\b", re.I)
DOWN = re.compile(r"\b(?:below|under|dip to|fall to|at most|less than|lower than|or fewer|or less)\b", re.I)
NUMBER = re.compile(r"(?P<prefix>[$€£]?)\s*(?P<num>\d[\d,]*(?:\.\d+)?)(?P<suffix>bps|bp|k|m|b|%)?(?![A-Za-z])", re.I)


@dataclass(frozen=True)
class TypedSignature:
    family: str
    direction: str
    threshold: float
    kind: str
    expiry_text: str


def _number(match: re.Match[str]) -> tuple[float, str]:
    raw = float(match.group("num").replace(",", ""))
    prefix = match.group("prefix") or ""
    suffix = (match.group("suffix") or "").lower()
    if suffix == "k": raw *= 1e3
    elif suffix == "m": raw *= 1e6
    elif suffix == "b": raw *= 1e9
    elif suffix == "%": raw /= 100.0
    elif suffix in {"bp", "bps"}: raw /= 10000.0
    if prefix: kind = "money"
    elif suffix == "%": kind = "percent"
    elif suffix in {"bp", "bps"}: kind = "bps"
    elif suffix in {"k", "m", "b"}: kind = "scaled_count"
    else: kind = "count"
    return raw, kind


def typed_signature(question: str) -> TypedSignature | None:
    up, down = UP.search(question), DOWN.search(question)
    if bool(up) == bool(down):
        return None
    direction_match = up or down
    assert direction_match is not None
    candidates = []
    for match in NUMBER.finditer(question):
        value, kind = _number(match)
        plain_year = kind == "count" and 1900 <= value <= 2100 and match.group("num").isdigit() and len(match.group("num")) == 4
        if plain_year:
            continue
        explicit = 2 if kind in {"money", "percent", "bps", "scaled_count"} else 1
        distance = min(abs(match.start() - direction_match.end()), abs(direction_match.start() - match.end()))
        candidates.append(((explicit, -distance), match, value, kind))
    if not candidates:
        return None
    _, chosen, threshold, kind = max(candidates, key=lambda x: x[0])
    text = question.lower()
    a, b = chosen.span()
    text = text[:a] + f" <threshold:{kind}> " + text[b:]
    text = UP.sub(" <direction> ", text)
    text = DOWN.sub(" <direction> ", text)
    dates = []
    for match in FULL_DATE.finditer(question):
        dates.append(f"{(match.group('year') or 'xxxx')}:{match.group('month').lower()[:3]}:{int(match.group('day')):02d}")
    expiry = ";".join(dates) if dates else ";".join(YEAR.findall(question))
    text = FULL_DATE.sub(" <expiry> ", text)
    text = YEAR.sub(" <year> ", text)
    text = re.sub(r"[^a-z<>:%]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return TypedSignature(f"{text}|kind={kind}|expiry={expiry}", "UP" if up else "DOWN", threshold, kind, expiry)


def structural_intents(markets: list[Any], books: dict[str, Any], now: int, min_edge: float, max_trade: float):
    families: dict[tuple[str, str, str, int], list[tuple[float, Any]]] = {}
    parse_rejects = 0
    for market in markets:
        sig = typed_signature(market.question)
        if sig is None:
            parse_rejects += 1
            continue
        # The market metadata end timestamp is part of the relation certificate.
        # Same textual date but different actual resolution timestamps is not a
        # guaranteed implication basket.
        expiry_ts = int(getattr(market, "end_ts", 0) or 0)
        if expiry_ts <= 0:
            parse_rejects += 1
            continue
        families.setdefault((sig.family, sig.direction, sig.kind, expiry_ts), []).append((sig.threshold, market))

    rows = []
    serial = 0
    considered = 0
    for (family, direction, kind, expiry_ts), values in families.items():
        values.sort(key=lambda x: x[0])
        for (lo_t, lo), (hi_t, hi) in zip(values, values[1:]):
            if not math.isfinite(lo_t) or not math.isfinite(hi_t) or hi_t <= lo_t:
                continue
            considered += 1
            if direction == "UP":
                token_legs = [(lo, "YES", lo.yes_token), (hi, "NO", hi.no_token)]
            else:
                token_legs = [(hi, "YES", hi.yes_token), (lo, "NO", lo.no_token)]
            if any(token not in books for _, _, token in token_legs):
                continue
            legs = [(m, side, books[token]) for m, side, token in token_legs]
            certificate = f"{family}|direction={direction}|kind={kind}|end_ts={expiry_ts}"
            event_id = "STRUCT_TYPED:" + str(abs(hash(certificate)))
            bundle = base.maker_bundle(now, "STRUCTURAL_TYPED", event_id, legs, min_edge, max_trade, serial)
            if bundle:
                rows.extend(bundle)
                serial += 1
    return rows, {
        "families": len(families),
        "relations_considered": considered,
        "bundles": serial,
        "parse_rejects": parse_rejects,
        "certificate": "typed_threshold+direction+unit+question_expiry+market_end_ts",
    }


def main() -> int:
    base.structural_intents = structural_intents
    return base.main()


if __name__ == "__main__":
    raise SystemExit(main())
