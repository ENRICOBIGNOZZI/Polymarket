#!/usr/bin/env python3
"""Validate physical London AZ identity without assuming cross-account letters."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


def validate_identity(config: dict[str, Any], *, region: str,
                      zone_name: str, zone_id: str) -> dict[str, str]:
    for key, required in (("paper_only", True), ("authenticated_execution", False),
                          ("real_order_submission", False), ("automatic_cutover", False)):
        if config.get(key) is not required:
            raise ValueError("PAPER_SAFETY_INVALID:" + key)
    if region != "eu-west-2" or config.get("region") != region:
        raise ValueError("REGION_MISMATCH")
    if not re.fullmatch(re.escape(region) + r"[a-z]", zone_name):
        raise ValueError("ACCOUNT_ZONE_NAME_INVALID")
    zones = config.get("zones")
    if not isinstance(zones, list) or not zones:
        raise ValueError("PHYSICAL_ZONE_ALLOWLIST_MISSING")
    approved: dict[str, str] = {}
    for row in zones:
        if not isinstance(row, dict):
            raise ValueError("PHYSICAL_ZONE_ALLOWLIST_INVALID")
        physical, support_name = row.get("zone_id"), row.get("zone_name")
        if not isinstance(physical, str) or not re.fullmatch(r"euw2-az[1-9][0-9]*", physical):
            raise ValueError("PHYSICAL_ZONE_ID_INVALID")
        if physical in approved:
            raise ValueError("DUPLICATE_PHYSICAL_ZONE_ID")
        if not isinstance(support_name, str):
            raise ValueError("SUPPORT_ZONE_NAME_MISSING")
        approved[physical] = support_name
    if zone_id not in approved:
        raise ValueError("PHYSICAL_ZONE_NOT_APPROVED")
    return {"region": region, "account_zone_name": zone_name,
            "zone_id": zone_id, "polymarket_account_zone_name": approved[zone_id],
            "zone_name_scope": "AWS_ACCOUNT_SPECIFIC",
            "verification": "PHYSICAL_ID_ALLOWLIST_MATCH"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--zone-name", required=True)
    parser.add_argument("--zone-id", required=True)
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("CONFIG_OBJECT_REQUIRED")
        result = validate_identity(config, region=args.region,
                                   zone_name=args.zone_name, zone_id=args.zone_id)
    except (OSError, ValueError) as exc:
        print(json.dumps({"passed": False, "reason": str(exc)}))
        return 2
    print(json.dumps({"passed": True, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
