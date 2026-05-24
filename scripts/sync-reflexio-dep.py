#!/usr/bin/env python3
"""Synchronize claude-smart's PyPI reflexio-ai dependency from Reflexio.

Use this for the strict PyPI release path. For fast npm releases that need
unpublished Reflexio changes, use scripts/vendor-reflexio.py via
scripts/release-with-reflexio.sh.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

PACKAGE_NAME = "reflexio-ai"
REPO_URL = "https://github.com/ReflexioAI/reflexio.git"
DEPENDENCY_RE = re.compile(r'"reflexio-ai[^"]*"')
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def run_git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        command = " ".join(("git", "-C", str(repo), *args))
        detail = (exc.stderr or exc.stdout or "").strip()
        fail(f"{command} failed: {detail or f'exit {exc.returncode}'}")
    return result.stdout.strip()


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


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


def dependency_for(version: str) -> str:
    if not VERSION_RE.match(version):
        fail(f"expected semantic version major.minor.patch, got {version!r}")
    return f"{PACKAGE_NAME}>={version}"


def check_pypi(version: str) -> None:
    url = f"https://pypi.org/pypi/{PACKAGE_NAME}/{version}/json"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "claude-smart-release"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status != 200:
                fail(
                    f"{PACKAGE_NAME}=={version} is not published on PyPI "
                    f"(HTTP {response.status})"
                )
    except urllib.error.HTTPError as exc:
        fail(f"{PACKAGE_NAME}=={version} is not published on PyPI (HTTP {exc.code})")
    except urllib.error.URLError as exc:
        fail(f"could not check PyPI for {PACKAGE_NAME}=={version}: {exc.reason}")


def check_release_git_state(reflexio_path: Path, version: str, commit: str) -> None:
    run_git(reflexio_path, "fetch", "origin", "main", "--tags")
    origin_main = run_git(reflexio_path, "rev-parse", "origin/main")
    if commit != origin_main:
        fail(
            "Reflexio checkout is not at origin/main:\n"
            f"  checkout HEAD: {commit}\n"
            f"  origin/main:   {origin_main}\n"
            "Release sync must run against the published Reflexio main commit."
        )

    expected_tag = f"v{version}"
    tags = set(run_git(reflexio_path, "tag", "--points-at", "HEAD").splitlines())
    if expected_tag not in tags:
        found = ", ".join(sorted(tags)) if tags else "none"
        fail(
            f"Reflexio HEAD is not tagged {expected_tag}. "
            f"Tags at HEAD: {found}. "
            "Tag the Reflexio release commit before syncing claude-smart."
        )


def updated_pyproject_text(pyproject: Path, dependency: str) -> tuple[str, bool]:
    text = pyproject.read_text()
    matches = DEPENDENCY_RE.findall(text)
    if len(matches) != 1:
        fail(
            f"expected exactly one quoted {PACKAGE_NAME} dependency in {pyproject}, "
            f"found {len(matches)}"
        )
    new_text = DEPENDENCY_RE.sub(f'"{dependency}"', text, count=1)
    return new_text, new_text != text


def current_lock_updated_at(
    lock_file: Path,
    version: str,
    commit: str,
    dependency: str,
) -> str | None:
    if not lock_file.is_file():
        return None
    try:
        payload = json.loads(lock_file.read_text())
    except json.JSONDecodeError:
        return None
    expected = {
        "package": PACKAGE_NAME,
        "repo": REPO_URL,
        "version": version,
        "commit": commit,
        "dependency": dependency,
        "source": "pypi",
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            return None
    updated_at = payload.get("updated_at")
    return updated_at if isinstance(updated_at, str) and updated_at else None


def build_lock(version: str, commit: str, dependency: str, updated_at: str | None) -> str:
    payload = {
        "package": PACKAGE_NAME,
        "repo": REPO_URL,
        "version": version,
        "commit": commit,
        "dependency": dependency,
        "source": "pypi",
        "updated_at": updated_at
        or datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    return json.dumps(payload, indent=2) + "\n"


def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reflexio-path",
        type=Path,
        default=repo_root.parent / "reflexio",
        help="Path to Reflexio checkout (default: ../reflexio relative to claude-smart)",
    )
    parser.add_argument("--check-pypi", action="store_true", help="Verify version exists on PyPI")
    parser.add_argument(
        "--release-checks",
        action="store_true",
        help="Require Reflexio HEAD to equal origin/main and be tagged v<version>",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow uncommitted changes in the Reflexio checkout",
    )
    parser.add_argument("--write", action="store_true", help="Modify files instead of dry-run")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    reflexio_path = args.reflexio_path.expanduser()
    if not reflexio_path.is_absolute():
        reflexio_path = (repo_root / reflexio_path).resolve()
    else:
        reflexio_path = reflexio_path.resolve()

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
    dependency = dependency_for(version)
    if args.release_checks:
        check_release_git_state(reflexio_path, version, commit)
    if args.check_pypi:
        check_pypi(version)

    pyproject = repo_root / "plugin" / "pyproject.toml"
    lock_file = repo_root / "reflexio.lock.json"
    new_pyproject, pyproject_changed = updated_pyproject_text(pyproject, dependency)
    updated_at = current_lock_updated_at(lock_file, version, commit, dependency)
    new_lock = build_lock(version, commit, dependency, updated_at)
    lock_changed = not lock_file.exists() or lock_file.read_text() != new_lock

    print(f"Reflexio path: {reflexio_path}")
    print(f"Reflexio package: {package}")
    print(f"Reflexio version: {version}")
    print(f"Reflexio commit: {commit}")
    print(f"New PyPI dependency: {dependency}")

    if args.write:
        if pyproject_changed:
            pyproject.write_text(new_pyproject)
            print(f"Updated: {pyproject.relative_to(repo_root)}")
        else:
            print(f"Unchanged: {pyproject.relative_to(repo_root)}")
        if lock_changed:
            lock_file.write_text(new_lock)
            print(f"Updated: {lock_file.relative_to(repo_root)}")
        else:
            print(f"Unchanged: {lock_file.relative_to(repo_root)}")
    else:
        print("Dry run only; no files changed.")
        print(
            "Would update:"
            f" {pyproject.relative_to(repo_root) if pyproject_changed else 'no pyproject change'},"
            f" {lock_file.relative_to(repo_root) if lock_changed else 'no lock change'}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
