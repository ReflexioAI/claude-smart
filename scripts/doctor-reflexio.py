#!/usr/bin/env python3
"""Diagnose whether claude-smart is using local, vendor, or PyPI Reflexio."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def fail(message: str, code: int = 1) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(code)


def run_import_probe(repo_root: Path) -> dict[str, str]:
    code = (
        "import json, pathlib, reflexio; "
        "print(json.dumps({"
        "'file': str(pathlib.Path(reflexio.__file__).resolve()), "
        "'version': str(getattr(reflexio, '__version__', 'unknown'))"
        "}))"
    )
    try:
        result = subprocess.run(
            [
                "uv",
                "run",
                "--project",
                str(repo_root / "plugin"),
                "--no-sync",
                "python",
                "-c",
                code,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        fail("uv is not on PATH; install uv or run scripts/setup-local-dev.sh")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        fail(
            "could not import reflexio from the claude-smart plugin environment. "
            "Run `uv sync --project plugin` or `bash scripts/use-local-reflexio.sh`.\n"
            f"{detail}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        fail(f"unexpected import probe output: {result.stdout!r}")


def classify_import(import_file: Path, repo_root: Path, reflexio_path: Path) -> str:
    local_package = reflexio_path / "reflexio"
    vendor_package = repo_root / "plugin" / "vendor" / "reflexio" / "reflexio"
    if import_file.is_relative_to(local_package):
        return "local"
    if import_file.is_relative_to(vendor_package):
        return "vendor"
    if "site-packages" in import_file.parts:
        return "pypi"
    return "unknown"


def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reflexio-path",
        type=Path,
        default=Path(os.environ.get("REFLEXIO_PATH", repo_root.parent / "reflexio")),
        help="Expected local Reflexio checkout (default: REFLEXIO_PATH or ../reflexio)",
    )
    parser.add_argument(
        "--allow-pypi",
        action="store_true",
        help="Exit 0 when the sibling Reflexio checkout exists but PyPI is active",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    reflexio_path = args.reflexio_path.expanduser()
    reflexio_path = (
        (repo_root / reflexio_path).resolve()
        if not reflexio_path.is_absolute()
        else reflexio_path.resolve()
    )
    probe = run_import_probe(repo_root)
    import_file = Path(probe["file"]).resolve()
    expected_file = reflexio_path / "reflexio" / "__init__.py"
    checkout_exists = (reflexio_path / "pyproject.toml").is_file() and (
        reflexio_path / "reflexio"
    ).is_dir()
    source = classify_import(import_file, repo_root, reflexio_path)

    print(f"Reflexio import path: {import_file}", flush=True)
    print(f"Reflexio version: {probe.get('version', 'unknown')}", flush=True)
    print(f"Expected local checkout: {reflexio_path}", flush=True)
    print(f"Expected local package: {expected_file}", flush=True)
    print(f"Detected source: {source}", flush=True)

    if source == "local":
        print("Status: OK - claude-smart is using the local editable Reflexio checkout.")
        return 0
    if source == "vendor":
        print("Status: OK - claude-smart is using the generated vendored Reflexio bundle.")
        return 0
    if checkout_exists and not args.allow_pypi:
        print(
            "Status: using PyPI/other Reflexio even though a local checkout exists."
        )
        print("Fix: bash scripts/use-local-reflexio.sh")
        return 1
    if checkout_exists:
        print(
            "Status: using PyPI/other Reflexio; local checkout exists but is not active."
        )
        print("Fix for cross-repo dev: bash scripts/use-local-reflexio.sh")
    else:
        print("Status: using PyPI/other Reflexio; no local sibling checkout was found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
