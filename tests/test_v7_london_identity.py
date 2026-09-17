#!/usr/bin/env python3
import copy
import itertools
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from v7_london_identity import validate_identity


class LondonPhysicalIdentityTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "config/v7_london_az_shootout.json").read_text())

    def test_all_cross_account_permutations_are_accepted(self):
        for ids in itertools.permutations(["euw2-az1", "euw2-az2", "euw2-az3"]):
            for letter, physical in zip("abc", ids):
                with self.subTest(letter=letter, physical=physical):
                    result = validate_identity(self.config, region="eu-west-2",
                                               zone_name="eu-west-2" + letter, zone_id=physical)
                    self.assertEqual(result["zone_id"], physical)

    def test_other_region_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "REGION_MISMATCH"):
            validate_identity(self.config, region="eu-west-1", zone_name="eu-west-1a", zone_id="euw2-az1")

    def test_unknown_physical_zone_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "PHYSICAL_ZONE_NOT_APPROVED"):
            validate_identity(self.config, region="eu-west-2", zone_name="eu-west-2a", zone_id="euw2-az99")

    def test_wrong_account_region_name_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "ACCOUNT_ZONE_NAME_INVALID"):
            validate_identity(self.config, region="eu-west-2", zone_name="eu-west-1a", zone_id="euw2-az1")

    def test_duplicate_allowlist_is_rejected(self):
        self.config["zones"].append(copy.deepcopy(self.config["zones"][0]))
        with self.assertRaisesRegex(ValueError, "DUPLICATE_PHYSICAL_ZONE_ID"):
            validate_identity(self.config, region="eu-west-2", zone_name="eu-west-2a", zone_id="euw2-az1")

    def test_real_authority_is_rejected(self):
        self.config["real_order_submission"] = True
        with self.assertRaisesRegex(ValueError, "PAPER_SAFETY_INVALID"):
            validate_identity(self.config, region="eu-west-2", zone_name="eu-west-2a", zone_id="euw2-az1")


if __name__ == "__main__":
    unittest.main()
