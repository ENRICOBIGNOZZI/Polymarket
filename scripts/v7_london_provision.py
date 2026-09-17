#!/usr/bin/env python3
"""Fail-closed EC2 provisioner for the V7 London PAPER AZ shootout."""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

SCHEMA = "polymarket_v7_london_provision_receipt_v1"
SHA40 = re.compile(r"^[0-9a-f]{40}$")
AMI = re.compile(r"^ami-[0-9a-f]+$")
SUBNET = re.compile(r"^subnet-[0-9a-f]+$")
SG = re.compile(r"^sg-[0-9a-f]+$")


class ProvisionError(RuntimeError):
    pass


def load_config(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ProvisionError("CONFIG_OBJECT_REQUIRED")
    return value

def validate_config(value: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    if (value.get("schema") != "polymarket_v7_london_az_shootout_v1"
            or value.get("region") != "eu-west-2"
            or value.get("paper_only") is not True
            or value.get("authenticated_execution") is not False
            or value.get("real_order_submission") is not False
            or value.get("automatic_cutover") is not False):
        raise ProvisionError("LONDON_CONFIG_SAFETY_INVALID")
    zones = value.get("zones")
    if not isinstance(zones, list) or len(zones) != 3:
        raise ProvisionError("THREE_APPROVED_ZONES_REQUIRED")
    zone_ids = [str(row.get("zone_id") or "") for row in zones if isinstance(row, dict)]
    if set(zone_ids) != {"euw2-az1", "euw2-az2", "euw2-az3"}:
        raise ProvisionError("PHYSICAL_ZONE_ALLOWLIST_INVALID")
    preferences = value.get("instance_type_preferences")
    if (not isinstance(preferences, list) or not preferences
            or any(not isinstance(item, str) or not item for item in preferences)):
        raise ProvisionError("INSTANCE_TYPE_PREFERENCES_REQUIRED")
    storage = value.get("storage_blueprint")
    if not isinstance(storage, dict):
        raise ProvisionError("STORAGE_BLUEPRINT_REQUIRED")
    for name in ("root", "data"):
        row = storage.get(name)
        if (not isinstance(row, dict) or row.get("type") != "gp3"
                or row.get("encrypted") is not True
                or type(row.get("size_gib")) is not int or int(row["size_gib"]) <= 0):
            raise ProvisionError("STORAGE_BLUEPRINT_INVALID")
    return str(value["region"]), zone_ids, list(preferences)


def parse_subnets(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in values:
        if "=" not in raw:
            raise ProvisionError("SUBNET_MAPPING_FORMAT")
        zone_id, subnet_id = raw.split("=", 1)
        if not zone_id or not SUBNET.fullmatch(subnet_id):
            raise ProvisionError("SUBNET_MAPPING_INVALID")
        if zone_id in result and result[zone_id] != subnet_id:
            raise ProvisionError("SUBNET_MAPPING_CONFLICT")
        result[zone_id] = subnet_id
    return result

def choose_common_instance_type(preferences: list[str],
                                offerings: dict[str, set[str]]) -> str:
    if not offerings:
        raise ProvisionError("INSTANCE_OFFERINGS_MISSING")
    for instance_type in preferences:
        if all(instance_type in values for values in offerings.values()):
            return instance_type
    raise ProvisionError("NO_COMMON_INSTANCE_TYPE_ACROSS_APPROVED_ZONES")


def plan(config: dict[str, Any], *, expected_sha: str,
         subnet_map: dict[str, str], security_group_id: str | None,
         key_name: str | None, iam_instance_profile: str | None) -> dict[str, Any]:
    region, zone_ids, preferences = validate_config(config)
    if not SHA40.fullmatch(expected_sha):
        raise ProvisionError("EXACT_SHA_REQUIRED")
    missing = sorted(set(zone_ids) - set(subnet_map))
    extra = sorted(set(subnet_map) - set(zone_ids))
    if missing or extra:
        raise ProvisionError("EXACT_PHYSICAL_ZONE_SUBNET_MAP_REQUIRED")
    if security_group_id is not None and not SG.fullmatch(security_group_id):
        raise ProvisionError("SECURITY_GROUP_ID_INVALID")
    if not key_name and not iam_instance_profile:
        raise ProvisionError("ADMIN_ACCESS_PATH_REQUIRED")
    return {
        "schema": "polymarket_v7_london_provision_plan_v1",
        "region": region, "expected_sha": expected_sha,
        "physical_zone_ids": sorted(zone_ids),
        "subnet_map": dict(sorted(subnet_map.items())),
        "instance_type_preferences": preferences,
        "security_group_id": security_group_id,
        "key_name": key_name, "iam_instance_profile": iam_instance_profile,
        "instance_count": 3, "paper_only": True,
        "authenticated_execution": False, "real_order_submission": False,
        "real_capital_at_risk": False, "automatic_cutover": False,
        "apply_required_for_side_effects": True,
    }

def _zone_names(ec2: Any, approved_ids: list[str]) -> dict[str, str]:
    response = ec2.describe_availability_zones(AllAvailabilityZones=True)
    mapping: dict[str, str] = {}
    for row in response.get("AvailabilityZones") or []:
        zone_id = str(row.get("ZoneId") or "")
        if zone_id in approved_ids and row.get("State") == "available":
            mapping[zone_id] = str(row.get("ZoneName") or "")
    if set(mapping) != set(approved_ids) or any(not value for value in mapping.values()):
        raise ProvisionError("APPROVED_PHYSICAL_ZONE_NOT_AVAILABLE_IN_ACCOUNT")
    return mapping


def _offerings(ec2: Any, zone_names: dict[str, str],
               preferences: list[str]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for zone_id, zone_name in zone_names.items():
        response = ec2.describe_instance_type_offerings(
            LocationType="availability-zone",
            Filters=[
                {"Name": "location", "Values": [zone_name]},
                {"Name": "instance-type", "Values": preferences},
            ],
        )
        result[zone_id] = {
            str(row.get("InstanceType") or "")
            for row in response.get("InstanceTypeOfferings") or []
            if row.get("InstanceType")
        }
    return result


def _validate_subnets(ec2: Any, subnet_map: dict[str, str]) -> tuple[dict[str, str], str]:
    response = ec2.describe_subnets(SubnetIds=list(subnet_map.values()))
    rows = response.get("Subnets") or []
    by_id = {str(row.get("SubnetId")): row for row in rows}
    vpcs: set[str] = set()
    for zone_id, subnet_id in subnet_map.items():
        row = by_id.get(subnet_id)
        if row is None or row.get("AvailabilityZoneId") != zone_id:
            raise ProvisionError("SUBNET_PHYSICAL_ZONE_MISMATCH")
        vpcs.add(str(row.get("VpcId") or ""))
    if len(vpcs) != 1 or "" in vpcs:
        raise ProvisionError("SUBNETS_MUST_SHARE_ONE_VPC")
    return {zone: str(by_id[subnet]["AvailabilityZone"])
            for zone, subnet in subnet_map.items()}, next(iter(vpcs))

def _validate_security_group(ec2: Any, security_group_id: str, vpc_id: str) -> None:
    response = ec2.describe_security_groups(GroupIds=[security_group_id])
    rows = response.get("SecurityGroups") or []
    if len(rows) != 1 or rows[0].get("VpcId") != vpc_id:
        raise ProvisionError("SECURITY_GROUP_VPC_MISMATCH")


def _resolve_ubuntu_ami(ssm: Any) -> str:
    name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
    response = ssm.get_parameter(Name=name)
    ami = str((response.get("Parameter") or {}).get("Value") or "")
    if not AMI.fullmatch(ami):
        raise ProvisionError("UBUNTU_2404_AMI_RESOLUTION_FAILED")
    return ami


def _root_device(ec2: Any, ami_id: str) -> str:
    response = ec2.describe_images(ImageIds=[ami_id])
    rows = response.get("Images") or []
    if len(rows) != 1:
        raise ProvisionError("AMI_IDENTITY_UNVERIFIABLE")
    root = str(rows[0].get("RootDeviceName") or "")
    if not root:
        raise ProvisionError("AMI_ROOT_DEVICE_MISSING")
    return root


def _block_devices(config: dict[str, Any], root_device: str) -> list[dict[str, Any]]:
    blueprint = config.get("storage_blueprint") or {}
    root = blueprint.get("root") or {}; data = blueprint.get("data") or {}
    return [
        {"DeviceName": root_device, "Ebs": {
            "VolumeType": str(root.get("type") or "gp3"),
            "VolumeSize": int(root.get("size_gib") or 0),
            "Encrypted": root.get("encrypted") is True, "DeleteOnTermination": True}},
        {"DeviceName": "/dev/sdf", "Ebs": {
            "VolumeType": str(data.get("type") or "gp3"),
            "VolumeSize": int(data.get("size_gib") or 0),
            "Encrypted": data.get("encrypted") is True, "DeleteOnTermination": True}},
    ]

def apply(config: dict[str, Any], provision_plan: dict[str, Any],
          *, receipt_path: Path) -> dict[str, Any]:
    try:
        import boto3
    except ImportError as exc:
        raise ProvisionError("BOTO3_REQUIRED") from exc
    session = boto3.Session(region_name=provision_plan["region"])
    if session.get_credentials() is None:
        raise ProvisionError("AWS_CREDENTIALS_REQUIRED")
    try:
        session.client("sts").get_caller_identity()
    except Exception as exc:
        raise ProvisionError("AWS_IDENTITY_REQUIRED") from exc
    ec2 = session.client("ec2"); ssm = session.client("ssm")
    zone_names = _zone_names(ec2, provision_plan["physical_zone_ids"])
    subnet_zone_names, vpc_id = _validate_subnets(ec2, provision_plan["subnet_map"])
    if any(subnet_zone_names[z] != zone_names[z] for z in zone_names):
        raise ProvisionError("SUBNET_ACCOUNT_ZONE_NAME_MISMATCH")
    security_group_id = provision_plan.get("security_group_id")
    if not security_group_id:
        raise ProvisionError("SECURITY_GROUP_ID_REQUIRED_FOR_APPLY")
    _validate_security_group(ec2, security_group_id, vpc_id)
    offerings = _offerings(ec2, zone_names, provision_plan["instance_type_preferences"])
    instance_type = choose_common_instance_type(
        provision_plan["instance_type_preferences"], offerings)
    ami_id = _resolve_ubuntu_ami(ssm)
    root_device = _root_device(ec2, ami_id)
    devices = _block_devices(config, root_device)

    launched: list[dict[str, str]] = []
    for zone_id in provision_plan["physical_zone_ids"]:
        request: dict[str, Any] = {
            "ImageId": ami_id, "InstanceType": instance_type,
            "MinCount": 1, "MaxCount": 1,
            "SubnetId": provision_plan["subnet_map"][zone_id],
            "SecurityGroupIds": [security_group_id],
            "Placement": {"AvailabilityZone": zone_names[zone_id]},
            "BlockDeviceMappings": devices,
            "MetadataOptions": {"HttpTokens": "required", "HttpEndpoint": "enabled",
                                "HttpPutResponseHopLimit": 1},
            "InstanceInitiatedShutdownBehavior": "stop",
            "TagSpecifications": [{"ResourceType": "instance", "Tags": [
                {"Key": "Name", "Value": f"polymarket-v7-paper-{zone_id}"},
                {"Key": "Project", "Value": "PolymarketV7"},
                {"Key": "Environment", "Value": "PAPER"},
                {"Key": "PhysicalAzId", "Value": zone_id},
                {"Key": "ExpectedSha", "Value": provision_plan["expected_sha"]},
            ]}],
        }
        if provision_plan.get("key_name"):
            request["KeyName"] = provision_plan["key_name"]
        if provision_plan.get("iam_instance_profile"):
            request["IamInstanceProfile"] = {"Name": provision_plan["iam_instance_profile"]}
        response = ec2.run_instances(**request)
        rows = response.get("Instances") or []
        if len(rows) != 1 or not rows[0].get("InstanceId"):
            raise ProvisionError("INSTANCE_LAUNCH_RESPONSE_INVALID")
        launched.append({"physical_zone_id": zone_id,
                         "account_zone_name": zone_names[zone_id],
                         "instance_id": str(rows[0]["InstanceId"])})

    receipt = {
        "schema": SCHEMA, "timestamp": int(time.time()),
        "region": provision_plan["region"],
        "code_sha": provision_plan["expected_sha"],
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "real_capital_at_risk": False,
        "automatic_cutover": False, "runtime_started": False,
        "instance_type": instance_type, "ami_id": ami_id,
        "instances": launched,
        "next_required_step": "BOOTSTRAP_THEN_SMOKE_THEN_24H_FORMAL_SHOOTOUT",
    }
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = receipt_path.with_suffix(receipt_path.suffix + ".tmp")
    tmp.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(receipt_path)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path,
                        default=Path("config/v7_london_az_shootout.json"))
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--subnet", action="append", default=[],
                        help="physical-zone-id=subnet-id; exactly one per approved zone")
    parser.add_argument("--security-group-id")
    parser.add_argument("--key-name")
    parser.add_argument("--iam-instance-profile")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--receipt", type=Path,
                        default=Path("london_provision_receipt.json"))
    args = parser.parse_args()

    config = load_config(args.config)
    provision_plan = plan(
        config, expected_sha=args.expected_sha,
        subnet_map=parse_subnets(args.subnet),
        security_group_id=args.security_group_id,
        key_name=args.key_name,
        iam_instance_profile=args.iam_instance_profile,
    )
    if not args.apply:
        print(json.dumps(provision_plan, indent=2, sort_keys=True))
        return 0
    receipt = apply(config, provision_plan, receipt_path=args.receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ProvisionError as exc:
        print(json.dumps({
            "schema": "polymarket_v7_london_provision_error_v1",
            "state": "BLOCKED", "reason": str(exc),
            "paper_only": True, "authenticated_execution": False,
            "real_order_submission": False, "real_capital_at_risk": False,
            "automatic_cutover": False,
        }, sort_keys=True), file=sys.stderr)
        raise SystemExit(2)
