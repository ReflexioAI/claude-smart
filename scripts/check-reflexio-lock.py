#!/usr/bin/env python3
"""Verify plugin/pyproject.toml matches reflexio.lock.json."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

DEPENDENCY_RE = re.compile(r'"reflexio-ai[^"]*"')
PROJECT_STRING_RE = re.compile(r'^([A-Za-z0-9_-]+)\s*=\s*"([^"]*)"\s*(?:#.*)?$')
PACKAGE_NAME = "reflexio-ai"
REPO_URL = "https://github.com/ReflexioAI/reflexio.git"
VALID_SOURCES = {"pypi", "vendor"}
VENDOR_MANIFEST = ".claude-smart-vendor.json"


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-vendor",
        action="store_true",
        help=(
            "If reflexio.lock.json uses source=vendor, require the generated "
            "vendor bundle"
        ),
    )
    return parser.parse_args()


def read_json_object(path: Path, label: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        fail(f"{label} is not valid JSON: {exc}")
    if not isinstance(payload, dict):
        fail(f"{label} top-level value must be a JSON object")
    return payload


def read_lock_file(lock_path: Path) -> dict[str, object]:
    return read_json_object(lock_path, "reflexio.lock.json")


def require_str(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        fail(f"reflexio.lock.json is missing a non-empty {key!r} field")
    return value


def read_vendor_version(vendor: Path) -> str:
    pyproject = vendor / "pyproject.toml"
    if not pyproject.is_file():
        fail(
            f"reflexio.lock.json requires vendored Reflexio but {vendor} is missing; "
            "run bash scripts/release-with-reflexio.sh before npm publish"
        )
    project: dict[str, str] = {}
    in_project = False
    for raw_line in pyproject.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_project = line == "[project]"
            continue
        if not in_project:
            continue
        match = PROJECT_STRING_RE.match(line)
        if match:
            project[match.group(1)] = match.group(2)

    name = project.get("name")
    version = project.get("version")
    if name != PACKAGE_NAME:
        fail(
            "vendored Reflexio package name mismatch: "
            f"expected {PACKAGE_NAME!r}, got {name!r}"
        )
    if not isinstance(version, str) or not version:
        fail(f"vendored Reflexio is missing [project].version in {pyproject}")
    return version


def read_vendor_manifest(vendor: Path) -> dict[str, object]:
    manifest = vendor / VENDOR_MANIFEST
    if not manifest.is_file():
        fail(
            f"vendored Reflexio metadata is missing at {manifest}; "
            "run bash scripts/release-with-reflexio.sh"
        )
    return read_json_object(manifest, str(manifest))


def vendor_content_fingerprint(vendor: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    file_count = 0
    for path in sorted(vendor.rglob("*"), key=lambda item: item.relative_to(vendor).as_posix()):
        relative = path.relative_to(vendor).as_posix()
        if relative == VENDOR_MANIFEST:
            continue
        if path.is_symlink():
            fail(f"vendored Reflexio contains unexpected symlink: {path}")
        if not path.is_file():
            continue
        data = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
        file_count += 1
    return digest.hexdigest(), file_count


def fix_command_for(source: str) -> str:
    if source == "vendor":
        return "Run: bash scripts/release-with-reflexio.sh"
    return "Run: REFLEXIO_RELEASE_SOURCE=pypi bash scripts/release-with-reflexio.sh"


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1].resolve()
    lock_path = repo_root / "reflexio.lock.json"
    pyproject_path = repo_root / "plugin" / "pyproject.toml"

    if not lock_path.is_file():
        fail("reflexio.lock.json is missing")
    lock_data = read_lock_file(lock_path)
    package = require_str(lock_data, "package")
    repo = require_str(lock_data, "repo")
    version = require_str(lock_data, "version")
    commit = require_str(lock_data, "commit")
    expected = require_str(lock_data, "dependency")
    source = require_str(lock_data, "source")

    if package != PACKAGE_NAME:
        fail(
            "reflexio.lock.json package mismatch: "
            f"expected {PACKAGE_NAME!r}, got {package!r}"
        )
    if repo != REPO_URL:
        fail(f"reflexio.lock.json repo mismatch: expected {REPO_URL!r}, got {repo!r}")
    if source not in VALID_SOURCES:
        fail(
            f"reflexio.lock.json source must be one of {sorted(VALID_SOURCES)}, "
            f"got {source!r}"
        )
    if len(commit) != 40 or not re.fullmatch(r"[0-9a-fA-F]+", commit):
        fail(f"reflexio.lock.json commit must be a full git SHA, got {commit!r}")
    if source == "pypi" and "vendor_path" in lock_data:
        fail("reflexio.lock.json source=pypi must not include vendor_path")

    pyproject_text = pyproject_path.read_text()
    matches = [match.strip('"') for match in DEPENDENCY_RE.findall(pyproject_text)]
    if len(matches) != 1:
        fail(
            f"expected exactly one quoted reflexio-ai dependency in {pyproject_path}, "
            f"found {len(matches)}"
        )
    actual = matches[0]
    if actual != expected:
        fail(
            "reflexio-ai dependency mismatch:\n"
            f"  plugin/pyproject.toml: {actual}\n"
            f"  reflexio.lock.json:   {expected}\n"
            f"{fix_command_for(source)}"
        )

    if source == "vendor":
        vendor_path = require_str(lock_data, "vendor_path")
        vendor = (repo_root / vendor_path).resolve()
        try:
            vendor_display = vendor.relative_to(repo_root)
        except ValueError:
            fail(f"vendor_path must stay within repo root: {vendor}")
        if args.check_vendor:
            vendor_version = read_vendor_version(vendor)
            if vendor_version != version:
                fail(
                    "vendored Reflexio version mismatch:\n"
                    f"  {vendor / 'pyproject.toml'}: {vendor_version}\n"
                    f"  reflexio.lock.json:       {version}\n"
                    "Run: bash scripts/release-with-reflexio.sh"
                )
            vendor_data = read_vendor_manifest(vendor)
            for key, expected_value in (
                ("package", package),
                ("repo", repo),
                ("version", version),
                ("commit", commit),
                ("dependency", expected),
            ):
                if vendor_data.get(key) != expected_value:
                    fail(
                        "vendored Reflexio metadata mismatch:\n"
                        f"  {vendor / VENDOR_MANIFEST} {key}: {vendor_data.get(key)!r}\n"
                        f"  reflexio.lock.json {key}: {expected_value!r}\n"
                        "Run: bash scripts/release-with-reflexio.sh"
                    )
            content_sha256, content_file_count = vendor_content_fingerprint(vendor)
            for key, expected_value in (
                ("content_sha256", content_sha256),
                ("content_file_count", content_file_count),
            ):
                if vendor_data.get(key) != expected_value:
                    fail(
                        "vendored Reflexio content mismatch:\n"
                        f"  {vendor / VENDOR_MANIFEST} {key}: {vendor_data.get(key)!r}\n"
                        f"  actual vendored Reflexio {key}: {expected_value!r}\n"
                        "Run: bash scripts/release-with-reflexio.sh"
                    )
            print(f"OK: vendored Reflexio bundle present at {vendor_display}")

    print(f"OK: {actual} ({source})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
