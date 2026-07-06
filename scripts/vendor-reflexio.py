#!/usr/bin/env python3
"""Vendor the current Reflexio checkout into the npm release payload.

This keeps claude-smart and Reflexio as independent repositories during normal
development, while allowing a claude-smart npm release to carry an exact
Reflexio source snapshot without requiring user machines to clone GitHub.

Both `make package` (local testing) and `make release-npm` vendor the Reflexio
submodule **as it is right now** — the working tree, so uncommitted edits are
captured too. `make release-npm` passes ``--require-clean`` so the vendored tree
equals the committed HEAD (reproducible, traceable via the recorded commit),
while `make package` allows a dirty tree and passes ``--bundle-only`` so it never
rewrites the tracked reflexio.lock.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

PACKAGE_NAME = "reflexio-ai"
REPO_URL = "https://github.com/ReflexioAI/reflexio.git"
VENDOR_PATH = Path("plugin/vendor/reflexio")
VENDOR_MANIFEST = ".claude-smart-vendor.json"
DEFAULT_INCLUDE = ["pyproject.toml", "README.md", "LICENSE", ".env.example", "reflexio"]


def fail(message: str) -> NoReturn:
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


VENDOR_IGNORE = shutil.ignore_patterns(
    ".git",
    ".venv",
    "__pycache__",
    "*.pyc",
    "*.pyo",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "*.egg-info",
)
REQUIRED_VENDOR_PATHS = ("pyproject.toml", "reflexio")


def safe_vendor_src(reflexio_path: Path, root: Path, rel: str) -> Path:
    """Resolve an include entry to a path guaranteed to sit inside the checkout.

    The include list is metadata-driven (Reflexio's own pyproject sdist config
    plus defaults), so guard against a malformed or hostile entry escaping the
    checkout via an absolute path, ``..`` traversal, or a symlinked top-level
    entry before it is copied into the published npm payload.

    Args:
        reflexio_path (Path): Root of the Reflexio checkout.
        root (Path): ``reflexio_path`` resolved (passed in to avoid re-resolving).
        rel (str): Include entry relative to the checkout root.

    Returns:
        Path: ``reflexio_path / rel``, validated to stay within the checkout.

    Raises:
        SystemExit: If the entry is absolute, contains ``..``, or resolves outside.
    """
    rel_path = Path(rel)
    if rel_path.is_absolute() or ".." in rel_path.parts:
        fail(f"vendored include path must stay inside the Reflexio checkout: {rel!r}")
    src = reflexio_path / rel_path
    resolved = src.resolve()
    if resolved != root and root not in resolved.parents:
        fail(f"vendored include path escapes the Reflexio checkout: {rel!r}")
    return src


def ignore_vendor_entries(src_dir: str, names: list[str]) -> set[str]:
    """copytree ignore filter: drop the usual junk plus any symlink.

    Dropping symlinks (rather than following them) keeps a link inside the
    Reflexio tree from dereferencing an out-of-checkout target into the vendored
    bundle. This matches the old ``git archive`` behaviour of never copying
    external file contents into the release payload.
    """
    ignored = set(VENDOR_IGNORE(src_dir, names))
    src_path = Path(src_dir)
    ignored.update(name for name in names if (src_path / name).is_symlink())
    return ignored


def copy_worktree(reflexio_path: Path, vendor_dest: Path, files: list[str]) -> None:
    """Copy the current Reflexio working tree into the vendor destination.

    Unlike a ``git archive`` of HEAD, this captures uncommitted edits too, so
    ``make package`` can vendor local Reflexio changes for testing. Callers that
    need reproducibility (release) pass ``--require-clean`` so the working tree
    equals the committed HEAD.

    Args:
        reflexio_path (Path): Root of the Reflexio checkout to copy from.
        vendor_dest (Path): Destination directory (wiped and recreated).
        files (list[str]): Top-level paths to include, relative to reflexio_path.

    Raises:
        SystemExit: If a required package path (pyproject.toml, reflexio) is missing.
    """
    root = reflexio_path.resolve()
    for required in REQUIRED_VENDOR_PATHS:
        if not (reflexio_path / required).exists():
            fail(f"Reflexio checkout is missing required path: {required}")
    if vendor_dest.exists():
        shutil.rmtree(vendor_dest)
    vendor_dest.mkdir(parents=True)
    for rel in files:
        src = safe_vendor_src(reflexio_path, root, rel)
        if not src.exists():
            continue
        dest = vendor_dest / rel
        if src.is_dir():
            # ignore_symlinks drops any symlink so the metadata-driven include
            # list cannot dereference a link into the published npm payload.
            shutil.copytree(src, dest, ignore=ignore_vendor_entries)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)


def vendor_content_fingerprint(vendor_dest: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    file_count = 0
    for path in sorted(vendor_dest.rglob("*"), key=lambda item: item.relative_to(vendor_dest).as_posix()):
        relative = path.relative_to(vendor_dest).as_posix()
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


def write_vendor_manifest(
    vendor_dest: Path,
    *,
    version: str,
    commit: str,
    dependency: str,
) -> None:
    content_sha256, content_file_count = vendor_content_fingerprint(vendor_dest)
    manifest = {
        "package": PACKAGE_NAME,
        "repo": REPO_URL,
        "version": version,
        "commit": commit,
        "dependency": dependency,
        "content_sha256": content_sha256,
        "content_file_count": content_file_count,
    }
    (vendor_dest / VENDOR_MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")


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
        "--require-clean",
        action="store_true",
        help=(
            "Fail if the Reflexio checkout has uncommitted changes. Used by release so the "
            "vendored working tree equals the committed HEAD. Omit for `make package`."
        ),
    )
    parser.add_argument(
        "--bundle-only",
        action="store_true",
        help="Regenerate only the vendor bundle; do not write reflexio.lock.json (local testing).",
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
    dirty = run_git(reflexio_path, "status", "--porcelain")
    if args.require_clean and dirty:
        fail(
            f"Reflexio checkout has uncommitted changes at {reflexio_path}. "
            "Commit/stash them or drop --require-clean (only `make package` allows a dirty tree)."
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
    if not args.bundle_only and dirty:
        print(
            "warning: Reflexio checkout is dirty; reflexio.lock.json will record HEAD "
            f"({commit[:12]}), which may not match the vendored working tree.",
            file=sys.stderr,
        )
    lock_changed = (
        None if args.bundle_only else write_lock(lock_file, version, commit, dependency, write=args.write)
    )

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
        copy_worktree(reflexio_path, vendor_dest, include_paths)
        write_vendor_manifest(
            vendor_dest,
            version=version,
            commit=commit,
            dependency=dependency,
        )
        print(f"Updated: {VENDOR_PATH}")
        if args.bundle_only:
            print("Skipped: reflexio.lock.json (--bundle-only)")
        else:
            print(f"{'Updated' if lock_changed else 'Unchanged'}: reflexio.lock.json")
    else:
        print("Dry run only; no files changed.")
        print(f"Would update: {VENDOR_PATH}")
        if args.bundle_only:
            print("Would skip: reflexio.lock.json (--bundle-only)")
        else:
            print(f"Would update: {'reflexio.lock.json' if lock_changed else 'no lock change'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
