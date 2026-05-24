#!/usr/bin/env python3
"""Vendor a Reflexio checkout into the npm release payload.

This keeps claude-smart and Reflexio as independent repositories during normal
development, while allowing a claude-smart npm release to carry an exact
Reflexio source snapshot without requiring user machines to clone GitHub.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path

PACKAGE_NAME = "reflexio-ai"
REPO_URL = "https://github.com/ReflexioAI/reflexio.git"
VENDOR_PATH = Path("plugin/vendor/reflexio")
DEFAULT_INCLUDE = ["pyproject.toml", "README.md", "LICENSE", ".env.example", "reflexio"]


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def run_git(repo: Path, *args: str, capture: bool = True) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        command = " ".join(("git", "-C", str(repo), *args))
        detail = (exc.stderr or exc.stdout or "").strip()
        fail(f"{command} failed: {detail or f'exit {exc.returncode}'}")
    return result.stdout.strip() if capture and result.stdout else ""


def read_project_metadata(reflexio_path: Path) -> tuple[str, str]:
    pyproject = reflexio_path / "pyproject.toml"
    if not pyproject.is_file():
        fail(f"{reflexio_path} does not contain pyproject.toml")
    with pyproject.open("rb") as handle:
        data = tomllib.load(handle)
    project = data.get("project", {})
    name = project.get("name")
    version = project.get("version")
    if name != PACKAGE_NAME:
        fail(f"expected [project].name = {PACKAGE_NAME!r}, found {name!r}")
    if not isinstance(version, str) or not version:
        fail("missing [project].version in Reflexio pyproject.toml")
    return name, version


def read_toml(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def package_include_paths(reflexio_path: Path) -> list[str]:
    pyproject = reflexio_path / "pyproject.toml"
    data = read_toml(pyproject)
    sdist = (
        data.get("tool", {})
        .get("hatch", {})
        .get("build", {})
        .get("targets", {})
        .get("sdist", {})
    )
    include = sdist.get("only-include")
    if not isinstance(include, list) or not all(isinstance(item, str) for item in include):
        return DEFAULT_INCLUDE
    required = {"pyproject.toml", "reflexio"}
    merged = list(dict.fromkeys([*include, "README.md", "LICENSE", ".env.example"]))
    missing_required = required.difference(merged)
    if missing_required:
        fail(
            "Reflexio sdist include list does not contain required package paths: "
            + ", ".join(sorted(missing_required))
        )
    return merged


def current_dependency(repo_root: Path) -> str:
    pyproject = repo_root / "plugin" / "pyproject.toml"
    text = pyproject.read_text()
    marker = '"reflexio-ai'
    matches = [
        line.strip().rstrip(",").strip('"')
        for line in text.splitlines()
        if line.strip().startswith(marker)
    ]
    if len(matches) != 1:
        fail(f"expected exactly one {PACKAGE_NAME} dependency in {pyproject}")
    dependency = matches[0]
    if " @ " in dependency or "file:" in dependency:
        fail(
            "plugin/pyproject.toml must keep a PyPI-safe reflexio-ai dependency; "
            "do not commit direct URL or local path dependencies"
        )
    return dependency


def export_reflexio(
    reflexio_path: Path,
    commit: str,
    vendor_dest: Path,
    files: list[str],
) -> None:
    with tempfile.TemporaryDirectory(prefix="claude-smart-reflexio-") as tmp:
        archive = Path(tmp) / "reflexio.tar"
        try:
            with archive.open("wb") as handle:
                subprocess.run(
                    ["git", "-C", str(reflexio_path), "archive", "--format=tar", commit, *files],
                    check=True,
                    stdout=handle,
                    stderr=subprocess.PIPE,
                    text=False,
                )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b"").decode(errors="replace").strip()
            fail(f"git archive failed: {detail or f'exit {exc.returncode}'}")
        if vendor_dest.exists():
            shutil.rmtree(vendor_dest)
        vendor_dest.mkdir(parents=True)
        with tarfile.open(archive) as tar:
            tar.extractall(vendor_dest)


def existing_updated_at(lock_file: Path, payload: dict[str, str]) -> str | None:
    if not lock_file.is_file():
        return None
    try:
        current = json.loads(lock_file.read_text())
    except json.JSONDecodeError:
        return None
    for key, value in payload.items():
        if current.get(key) != value:
            return None
    updated_at = current.get("updated_at")
    return updated_at if isinstance(updated_at, str) and updated_at else None


def write_lock(
    lock_file: Path,
    version: str,
    commit: str,
    dependency: str,
    *,
    write: bool,
) -> bool:
    base_payload = {
        "package": PACKAGE_NAME,
        "repo": REPO_URL,
        "version": version,
        "commit": commit,
        "dependency": dependency,
        "source": "vendor",
        "vendor_path": str(VENDOR_PATH),
    }
    updated_at = existing_updated_at(lock_file, base_payload)
    payload = {
        **base_payload,
        "updated_at": updated_at
        or datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    new_text = json.dumps(payload, indent=2) + "\n"
    changed = not lock_file.exists() or lock_file.read_text() != new_text
    if write and changed:
        lock_file.write_text(new_text)
    return changed


def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reflexio-path",
        type=Path,
        default=repo_root.parent / "reflexio",
        help="Path to Reflexio checkout (default: ../reflexio relative to claude-smart)",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow uncommitted Reflexio changes. The committed HEAD is still what gets vendored.",
    )
    parser.add_argument(
        "--require-origin-main",
        action="store_true",
        help="Fail unless Reflexio HEAD matches origin/main after fetching.",
    )
    parser.add_argument("--write", action="store_true", help="Create vendor files and update lock")
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
    if not reflexio_path.exists():
        fail(f"Reflexio path does not exist: {reflexio_path}")

    package, version = read_project_metadata(reflexio_path)
    if not args.allow_dirty:
        dirty = run_git(reflexio_path, "status", "--porcelain")
        if dirty:
            fail(
                f"Reflexio checkout has uncommitted changes at {reflexio_path}. "
                "Commit/stash them or pass --allow-dirty."
            )
    commit = run_git(reflexio_path, "rev-parse", "HEAD")
    if args.require_origin_main:
        run_git(reflexio_path, "fetch", "origin", "main", capture=False)
        origin_main = run_git(reflexio_path, "rev-parse", "origin/main")
        if commit != origin_main:
            fail(
                "Reflexio checkout is not at origin/main:\n"
                f"  checkout HEAD: {commit}\n"
                f"  origin/main:   {origin_main}"
            )

    dependency = current_dependency(repo_root)
    include_paths = package_include_paths(reflexio_path)
    vendor_dest = repo_root / VENDOR_PATH
    lock_file = repo_root / "reflexio.lock.json"
    lock_changed = write_lock(lock_file, version, commit, dependency, write=args.write)

    print(f"Reflexio path: {reflexio_path}")
    print(f"Reflexio package: {package}")
    print(f"Reflexio version: {version}")
    print(f"Reflexio commit: {commit}")
    print(f"PyPI fallback dependency: {dependency}")
    print(f"Vendor path: {VENDOR_PATH}")
    print("Vendored paths:")
    for include_path in include_paths:
        print(f"  {include_path}")

    if args.write:
        export_reflexio(reflexio_path, commit, vendor_dest, include_paths)
        print(f"Updated: {VENDOR_PATH}")
        print(f"{'Updated' if lock_changed else 'Unchanged'}: reflexio.lock.json")
    else:
        print("Dry run only; no files changed.")
        print(f"Would update: {VENDOR_PATH}")
        print(f"Would update: {'reflexio.lock.json' if lock_changed else 'no lock change'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
