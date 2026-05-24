#!/usr/bin/env python3
"""Verify plugin/pyproject.toml matches reflexio.lock.json."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DEPENDENCY_RE = re.compile(r'"reflexio-ai[^"]*"')


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
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

    print(f"OK: {actual}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
