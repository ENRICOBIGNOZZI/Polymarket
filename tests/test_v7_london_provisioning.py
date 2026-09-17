from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "infra/aws/v7_london_shootout.json"
PROVISION = ROOT / "ops/v7_london_provision.sh"
POLICY = ROOT / "config/v7_london_az_shootout.json"
BOOTSTRAP = ROOT / "ops/v7_london_bootstrap.sh"


class LondonProvisioningContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        self.resources = self.template["Resources"]

    def test_three_physical_zone_ids_are_explicit(self) -> None:
        zones = {
            self.resources[name]["Properties"]["AvailabilityZoneId"]
            for name in ("SubnetAz1", "SubnetAz2", "SubnetAz3")
        }
        self.assertEqual(zones, {"euw2-az1", "euw2-az2", "euw2-az3"})

    def test_hosts_are_identical_and_require_imdsv2(self) -> None:
        for name in ("HostAz1", "HostAz2", "HostAz3"):
            props = self.resources[name]["Properties"]
            self.assertEqual(props["ImageId"], {"Ref": "ImageId"})
            self.assertEqual(props["InstanceType"], {"Ref": "InstanceType"})
            self.assertEqual(props["MetadataOptions"]["HttpTokens"], "required")
            self.assertTrue(props["EbsOptimized"])
            self.assertTrue(props["Monitoring"])
            self.assertNotIn("KeyName", props)
            volumes = [x["Ebs"] for x in props["BlockDeviceMappings"]]
            self.assertEqual([(v["VolumeType"], v["VolumeSize"], v["Encrypted"])
                              for v in volumes], [("gp3", 40, True), ("gp3", 250, True)])

    def test_security_group_has_no_inbound_rules(self) -> None:
        props = self.resources["EgressOnlySecurityGroup"]["Properties"]
        self.assertEqual(props["SecurityGroupIngress"], [])
        self.assertEqual(props["SecurityGroupEgress"], [{"IpProtocol": "-1", "CidrIp": "0.0.0.0/0"}])

    def test_admin_plane_uses_ssm_without_embedded_credentials(self) -> None:
        role = self.resources["InstanceRole"]["Properties"]
        self.assertEqual(role["ManagedPolicyArns"], ["arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"])
        raw = TEMPLATE.read_text(encoding="utf-8")
        for forbidden in ("TS_AUTHKEY", "AWS_SECRET_ACCESS_KEY", "Authorization:", "Password"):
            self.assertNotIn(forbidden, raw)

    def test_userdata_bootstraps_exact_sha_but_never_enables_runtime(self) -> None:
        data = self.resources["HostAz1"]["Properties"]["UserData"]["Fn::Base64"]["Fn::Sub"]
        self.assertIn("${ExpectedSha}", data)
        self.assertIn("v7_london_bootstrap.sh", data)
        self.assertIn("/mnt/polymarket-data/paper_v7_london", data)
        self.assertNotIn("systemctl enable --now polymarket-v7-paper", data)
        self.assertNotIn("tailscale up", data)
    def test_provisioner_verifies_identity_zone_ids_offerings_and_canonical_ami(self) -> None:
        source = PROVISION.read_text(encoding="utf-8")
        for required in (
            "aws sts get-caller-identity",
            "describe-availability-zones",
            "euw2-az1",
            "euw2-az2",
            "euw2-az3",
            "describe-instance-type-offerings",
            "--owners 099720109477",
            "ubuntu-noble-24.04-amd64-server-",
            "aws cloudformation deploy",
        ):
            self.assertIn(required, source)
        self.assertNotIn("readarray", source)
        self.assertNotIn("mapfile", source)
        self.assertNotIn("TS_AUTHKEY", source)

    def test_hft_instance_policy_requires_real_cores_without_smt(self) -> None:
        policy = json.loads(POLICY.read_text(encoding="utf-8"))
        constraints = policy["instance_constraints"]
        self.assertGreaterEqual(constraints["minimum_physical_cores"], 4)
        self.assertEqual(constraints["maximum_threads_per_core"], 1)
        self.assertGreaterEqual(constraints["minimum_vcpus"], 4)
        preferences = policy["instance_type_preferences"]
        self.assertGreaterEqual(len(preferences), 2)
        self.assertTrue(all(x.startswith(("c8a.", "c7a.")) for x in preferences))
        self.assertTrue(preferences[0].endswith("2xlarge"))

    def test_provisioner_checks_actual_instance_cpu_topology(self) -> None:
        source = PROVISION.read_text(encoding="utf-8")
        for required in (
            "describe-instance-types",
            "DefaultCores",
            "DefaultThreadsPerCore",
            "DefaultVCpus",
            "minimum_physical_cores",
            "maximum_threads_per_core",
            "instance_topology",
        ):
            self.assertIn(required, source)

    def test_wrong_region_fails_before_aws_access(self) -> None:
        env = os.environ.copy()
        env.update(POLYMARKET_EXPECTED_SHA="1" * 40, AWS_REGION="us-east-1")
        completed = subprocess.run(["/bin/bash", str(PROVISION)], env=env,
                                   text=True, capture_output=True, check=False)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("requires eu-west-2", completed.stderr)

    def test_bootstrap_keeps_runtime_disabled(self) -> None:
        source = BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn("systemctl disable --now polymarket-v7-paper.service", source)
        self.assertIn("tailscale_authenticated':False", source)


if __name__ == "__main__":
    unittest.main()
