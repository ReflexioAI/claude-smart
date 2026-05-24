#!/usr/bin/env python3
"""Verify plugin/pyproject.toml matches reflexio.lock.json."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DEPENDENCY_RE = re.compile(r'"reflexio-ai[^"]*"')


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-vendor",
        action="store_true",
        help="If reflexio.lock.json uses source=vendor, require the generated vendor bundle",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    lock_path = repo_root / "reflexio.lock.json"
    pyproject_path = repo_root / "plugin" / "pyproject.toml"

    if not lock_path.is_file():
        fail("reflexio.lock.json is missing")
    lock_data = json.loads(lock_path.read_text())
    expected = lock_data.get("dependency")
    if not isinstance(expected, str) or not expected:
        fail("reflexio.lock.json is missing a non-empty dependency field")

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
            "Run: python scripts/sync-reflexio-dep.py --write"
        )

    if args.check_vendor and lock_data.get("source") == "vendor":
        vendor = repo_root / str(lock_data.get("vendor_path") or "plugin/vendor/reflexio")
        if not (vendor / "pyproject.toml").is_file():
            fail(
                f"reflexio.lock.json requires vendored Reflexio but {vendor} is missing; "
                "run bash scripts/release-with-reflexio.sh before npm publish"
            )
        print(f"OK: vendored Reflexio bundle present at {vendor.relative_to(repo_root)}")

    print(f"OK: {actual}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
