#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_london_provision import (ProvisionError, apply, choose_common_instance_type,
                                 load_config, parse_subnets, plan)

SHA = "a" * 40
ZONE_IDS = ("euw2-az1", "euw2-az2", "euw2-az3")
SUBNETS = {zone: f"subnet-{index + 1:08x}" for index, zone in enumerate(ZONE_IDS)}
SG = "sg-12345678"


class FakeEc2:
    def __init__(self):
        self.run_calls = []

    def describe_availability_zones(self, **_kwargs):
        return {"AvailabilityZones": [
            {"ZoneId": zone, "ZoneName": f"eu-west-2{letter}", "State": "available"}
            for zone, letter in zip(ZONE_IDS, "cab")
        ]}

    def describe_instance_type_offerings(self, **kwargs):
        assert kwargs["LocationType"] == "availability-zone"
        return {"InstanceTypeOfferings": [
            {"InstanceType": "c7i.large"}, {"InstanceType": "c6i.large"}
        ]}

    def describe_subnets(self, **kwargs):
        assert set(kwargs["SubnetIds"]) == set(SUBNETS.values())
        return {"Subnets": [
            {"SubnetId": SUBNETS[zone], "AvailabilityZoneId": zone,
             "AvailabilityZone": f"eu-west-2{letter}", "VpcId": "vpc-12345678"}
            for zone, letter in zip(ZONE_IDS, "cab")
        ]}

    def describe_security_groups(self, **kwargs):
        assert kwargs["GroupIds"] == [SG]
        return {"SecurityGroups": [{"GroupId": SG, "VpcId": "vpc-12345678"}]}

    def describe_images(self, **kwargs):
        return {"Images": [{"ImageId": kwargs["ImageIds"][0],
                            "RootDeviceName": "/dev/sda1"}]}

    def run_instances(self, **kwargs):
        self.run_calls.append(kwargs)
        return {"Instances": [{"InstanceId": f"i-{len(self.run_calls):08x}"}]}


class FakeSsm:
    def get_parameter(self, **_kwargs):
        return {"Parameter": {"Value": "ami-12345678"}}


class FakeSession:
    ec2 = FakeEc2()

    def __init__(self, **kwargs):
        assert kwargs["region_name"] == "eu-west-2"

    def get_credentials(self):
        return object()

    def client(self, name, **_kwargs):
        if name == "sts":
            return types.SimpleNamespace(get_caller_identity=lambda: {"Account": "redacted"})
        if name == "ec2": return self.ec2
        if name == "ssm": return FakeSsm()
        raise AssertionError(name)


class LondonProvisionTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(ROOT / "config/v7_london_az_shootout.json")

    def test_plan_is_exact_three_zone_paper_only_and_side_effect_free(self):
        value = plan(self.config, expected_sha=SHA, subnet_map=SUBNETS,
                     security_group_id=SG, key_name="admin-key",
                     iam_instance_profile=None)
        self.assertEqual(value["physical_zone_ids"], sorted(ZONE_IDS))
        self.assertEqual(value["instance_count"], 3)
        self.assertTrue(value["paper_only"])
        self.assertFalse(value["real_order_submission"])
        self.assertTrue(value["apply_required_for_side_effects"])

    def test_plan_requires_admin_path_and_exact_subnet_map(self):
        with self.assertRaisesRegex(ProvisionError, "ADMIN_ACCESS_PATH_REQUIRED"):
            plan(self.config, expected_sha=SHA, subnet_map=SUBNETS,
                 security_group_id=SG, key_name=None, iam_instance_profile=None)
        broken = dict(SUBNETS); broken.pop("euw2-az1")
        with self.assertRaisesRegex(ProvisionError, "EXACT_PHYSICAL_ZONE_SUBNET_MAP_REQUIRED"):
            plan(self.config, expected_sha=SHA, subnet_map=broken,
                 security_group_id=SG, key_name="key", iam_instance_profile=None)

    def test_common_instance_type_must_exist_in_every_zone(self):
        offerings = {zone: {"c7i.large", "c6i.large"} for zone in ZONE_IDS}
        self.assertEqual(choose_common_instance_type(
            ["c7a.large", "c7i.large", "c6i.large"], offerings), "c7i.large")
        offerings["euw2-az3"] = {"c6i.large"}
        self.assertEqual(choose_common_instance_type(
            ["c7i.large", "c6i.large"], offerings), "c6i.large")
        offerings["euw2-az2"] = {"c7i.large"}
        with self.assertRaisesRegex(ProvisionError, "NO_COMMON_INSTANCE_TYPE"):
            choose_common_instance_type(["c7i.large", "c6i.large"], offerings)

    def test_apply_launches_exactly_one_host_per_physical_zone_and_starts_nothing(self):
        provision_plan = plan(
            self.config, expected_sha=SHA, subnet_map=SUBNETS,
            security_group_id=SG, key_name="admin-key", iam_instance_profile=None)
        FakeSession.ec2 = FakeEc2()
        fake_boto3 = types.SimpleNamespace(Session=FakeSession)
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(sys.modules, {"boto3": fake_boto3}):
            receipt = apply(self.config, provision_plan,
                            receipt_path=Path(directory) / "receipt.json")
        self.assertEqual(len(receipt["instances"]), 3)
        self.assertEqual(len(FakeSession.ec2.run_calls), 3)
        self.assertFalse(receipt["runtime_started"])
        self.assertFalse(receipt["automatic_cutover"])
        self.assertEqual({row["physical_zone_id"] for row in receipt["instances"]},
                         set(ZONE_IDS))
        for call in FakeSession.ec2.run_calls:
            self.assertEqual(call["InstanceType"], "c7i.large")
            self.assertEqual(call["MetadataOptions"]["HttpTokens"], "required")
            self.assertEqual(call["SecurityGroupIds"], [SG])


if __name__ == "__main__":
    unittest.main()
