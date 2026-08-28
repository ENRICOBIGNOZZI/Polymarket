#!/usr/bin/env python3
"""Create and validate V7 build manifests with binary hashes and an SBOM."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = "polymarket_v7_build_manifest_v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
BUILD_TYPES = {"Release", "Debug", "RelWithDebInfo", "MinSizeRel"}


class ManifestError(ValueError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str], *, required: bool = True) -> str:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        if required:
            raise ManifestError(f"command_failed:{shlex.join(command)}:{exc}") from exc
        return "unavailable"
    output = (result.stdout or result.stderr).strip()
    if result.returncode != 0:
        if required:
            raise ManifestError(f"command_failed:{shlex.join(command)}")
        return "unavailable"
    return output


def git_sha(repository_root: Path) -> str:
    value = _run(["git", "-C", str(repository_root), "rev-parse", "HEAD"])
    if not GIT_SHA_RE.fullmatch(value):
        raise ManifestError("code_sha:not_exact_git_sha")
    return value


def _tool_identity(command: str) -> dict[str, str]:
    executable = shlex.split(command)
    if not executable:
        raise ManifestError("compiler:empty")
    version = _run([*executable, "--version"])
    return {"command": command, "version": version.splitlines()[0] if version else "unknown"}


def _dependency(name: str, package: str) -> dict[str, str]:
    version = _run(["pkg-config", "--modversion", package], required=False).splitlines()[0]
    return {"name": name, "version": version, "discovery": f"pkg-config:{package}"}


def _boost_dependency() -> dict[str, str]:
    candidates = [
        Path("/usr/include/boost/version.hpp"),
        Path("/usr/local/include/boost/version.hpp"),
    ]
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        match = re.search(r'^\s*#\s*define\s+BOOST_LIB_VERSION\s+"([^"]+)"', text, re.MULTILINE)
        if match:
            return {
                "name": "Boost",
                "version": match.group(1).replace("_", "."),
                "discovery": path.as_posix(),
            }
    return {"name": "Boost", "version": "unavailable", "discovery": "boost/version.hpp"}


def _iso_time(value: str | None) -> str:
    if value is None:
        epoch = os.environ.get("SOURCE_DATE_EPOCH")
        moment = datetime.fromtimestamp(int(epoch), tz=timezone.utc) if epoch else datetime.now(timezone.utc)
        return moment.isoformat(timespec="seconds").replace("+00:00", "Z")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestError("timestamp:invalid_iso8601") from exc
    if parsed.tzinfo is None:
        raise ManifestError("timestamp:timezone_required")
    return value


def _display_path(path: Path, base: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(base.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def build_manifest(
    *,
    binaries: list[Path],
    code_sha: str,
    build_type: str,
    compiler: str,
    repository_root: Path,
    timestamp: str | None = None,
    build_flags: list[str] | None = None,
    extra_dependencies: list[str] | None = None,
) -> dict[str, Any]:
    if not GIT_SHA_RE.fullmatch(code_sha):
        raise ManifestError("code_sha:not_exact_git_sha")
    if build_type not in BUILD_TYPES:
        raise ManifestError("build_type:invalid")
    if not binaries:
        raise ManifestError("binaries:empty")
    base = repository_root.resolve()
    entries: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for raw_path in binaries:
        path = raw_path.resolve()
        if path in seen:
            raise ManifestError(f"binary:{raw_path}:duplicate")
        seen.add(path)
        if not path.is_file():
            raise ManifestError(f"binary:{raw_path}:not_a_file")
        entries.append(
            {
                "path": _display_path(path, base),
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        )
    entries.sort(key=lambda row: row["path"])
    dependencies = [
        _boost_dependency(),
        _dependency("libcurl", "libcurl"),
        _dependency("OpenSSL", "openssl"),
        {"name": "Python", "version": platform.python_version(), "discovery": "runtime"},
    ]
    for item in extra_dependencies or []:
        name, separator, package = item.partition("=")
        if not separator or not name or not package:
            raise ManifestError("dependency:expected_NAME=PKG_CONFIG_PACKAGE")
        dependencies.append(_dependency(name, package))
    dependencies.sort(key=lambda row: row["name"].lower())

    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "code_sha": code_sha,
        "compiler": _tool_identity(compiler),
        "cmake": _tool_identity("cmake"),
        "build_type": build_type,
        "build_flags": list(build_flags or []),
        "dependencies": dependencies,
        "binaries": entries,
        "binary_sha256": hashlib.sha256(canonical_bytes(entries)).hexdigest(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "libc": " ".join(part for part in platform.libc_ver() if part),
        },
        "timestamp": _iso_time(timestamp),
        "sbom": {
            "format": "CycloneDX",
            "spec_version": "1.5",
            "components": [
                {"type": "library", "name": row["name"], "version": row["version"]}
                for row in dependencies
            ],
        },
    }
    manifest["manifest_sha256"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    validate_manifest(manifest)
    return manifest


def validate_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError("manifest:not_an_object")
    required = {
        "schema", "code_sha", "compiler", "cmake", "build_type", "build_flags",
        "dependencies", "binaries", "binary_sha256", "platform", "timestamp", "sbom",
        "manifest_sha256",
    }
    missing = sorted(required - value.keys())
    if missing:
        raise ManifestError("manifest:missing:" + ",".join(missing))
    if value["schema"] != SCHEMA:
        raise ManifestError("schema:unsupported")
    if not GIT_SHA_RE.fullmatch(str(value["code_sha"])):
        raise ManifestError("code_sha:not_exact_git_sha")
    if value["build_type"] not in BUILD_TYPES:
        raise ManifestError("build_type:invalid")
    for tool in ("compiler", "cmake"):
        identity = value[tool]
        if not isinstance(identity, dict) or set(identity) != {"command", "version"}:
            raise ManifestError(f"{tool}:invalid")
        if not identity["command"] or not identity["version"]:
            raise ManifestError(f"{tool}:empty")
    binaries = value["binaries"]
    if not isinstance(binaries, list) or not binaries:
        raise ManifestError("binaries:empty")
    for binary in binaries:
        if not isinstance(binary, dict) or set(binary) != {"path", "sha256", "size_bytes"}:
            raise ManifestError("binaries:invalid_entry")
        if not binary["path"] or not SHA256_RE.fullmatch(str(binary["sha256"])):
            raise ManifestError("binaries:invalid_identity")
        if int(binary["size_bytes"]) < 1:
            raise ManifestError("binaries:empty_file")
    expected_binary_hash = hashlib.sha256(canonical_bytes(binaries)).hexdigest()
    if value["binary_sha256"] != expected_binary_hash:
        raise ManifestError("binary_sha256:mismatch")
    if not isinstance(value["dependencies"], list):
        raise ManifestError("dependencies:invalid")
    for dependency in value["dependencies"]:
        if not isinstance(dependency, dict) or set(dependency) != {"name", "version", "discovery"}:
            raise ManifestError("dependencies:invalid_entry")
        if not dependency["name"] or not dependency["version"]:
            raise ManifestError("dependencies:empty_identity")
    _iso_time(str(value["timestamp"]))
    sbom = value["sbom"]
    if not isinstance(sbom, dict) or sbom.get("format") != "CycloneDX" or sbom.get("spec_version") != "1.5":
        raise ManifestError("sbom:invalid")
    supplied_hash = str(value["manifest_sha256"])
    unhashed = dict(value)
    unhashed.pop("manifest_sha256")
    expected_hash = hashlib.sha256(canonical_bytes(unhashed)).hexdigest()
    if supplied_hash != expected_hash:
        raise ManifestError("manifest_sha256:mismatch")
    return value


def immutable_write(path: Path, value: dict[str, Any]) -> None:
    payload = canonical_bytes(value) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise ManifestError("output:immutable_path_collision")
        return
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create", help="create a build manifest")
    create.add_argument("--output", type=Path, default=Path("build_manifest.json"))
    create.add_argument("--binary", action="append", type=Path, required=True)
    create.add_argument("--repository-root", type=Path, default=Path.cwd())
    create.add_argument("--code-sha")
    create.add_argument("--build-type", choices=sorted(BUILD_TYPES), required=True)
    create.add_argument("--compiler", default=os.environ.get("CXX", "c++"))
    create.add_argument("--build-flag", action="append", default=[])
    create.add_argument("--dependency", action="append", default=[], metavar="NAME=PKG")
    create.add_argument("--timestamp")
    validate = subparsers.add_parser("validate", help="validate a build manifest")
    validate.add_argument("manifest", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            value = validate_manifest(json.loads(args.manifest.read_text(encoding="utf-8")))
            print(json.dumps({"valid": True, "code_sha": value["code_sha"], "manifest_sha256": value["manifest_sha256"]}, sort_keys=True))
            return 0
        code_sha = args.code_sha or git_sha(args.repository_root)
        value = build_manifest(
            binaries=args.binary,
            code_sha=code_sha,
            build_type=args.build_type,
            compiler=args.compiler,
            repository_root=args.repository_root,
            timestamp=args.timestamp,
            build_flags=args.build_flag,
            extra_dependencies=args.dependency,
        )
        immutable_write(args.output, value)
        print(json.dumps({"build_manifest": str(args.output), "code_sha": value["code_sha"], "manifest_sha256": value["manifest_sha256"]}, sort_keys=True))
        return 0
    except (ManifestError, OSError, json.JSONDecodeError) as exc:
        print(f"v7_build_manifest: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
