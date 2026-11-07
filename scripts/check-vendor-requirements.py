#!/usr/bin/env python3
"""Verify plugin/uv.lock installs every runtime requirement of the vendored Reflexio.

Installs run ``uv sync --locked`` (which installs the PyPI ``reflexio-ai``
baseline and its dependencies) and then install ``plugin/vendor/reflexio`` over
it with ``--no-deps``. Any requirement the vendored source adds or raises beyond
the PyPI baseline is therefore never installed unless plugin/uv.lock already
provides it. This check reads the vendored ``pyproject.toml``
``[project].dependencies`` and, for each supported install target, walks the
locked dependency graph from ``claude-smart`` to confirm every requirement
(name, version specifier and requested extras) is installed by the lock.

With no vendor bundle present (a PyPI-only release, or a source checkout), the
installed ``reflexio-ai`` is the locked one, whose dependencies uv resolves
itself, so the check passes trivially.

Run with an interpreter that has ``packaging`` (the plugin venv does)::

    uv --project plugin run python scripts/check-vendor-requirements.py
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VENDOR = REPO_ROOT / "plugin" / "vendor" / "reflexio"
DEFAULT_LOCK = REPO_ROOT / "plugin" / "uv.lock"
ROOT_PACKAGE = "claude-smart"
PYTHON_FULL_VERSION = "3.12.0"


@dataclass(frozen=True)
class Target:
    """One supported install platform (installers always pass ``--python 3.12``)."""

    label: str
    sys_platform: str
    platform_system: str
    platform_machine: str
    os_name: str

    def environment(self, extra: str = "") -> dict[str, str]:
        return {
            "implementation_name": "cpython",
            "implementation_version": PYTHON_FULL_VERSION,
            "os_name": self.os_name,
            "platform_machine": self.platform_machine,
            "platform_python_implementation": "CPython",
            "platform_release": "",
            "platform_system": self.platform_system,
            "platform_version": "",
            "python_full_version": PYTHON_FULL_VERSION,
            "python_version": PYTHON_FULL_VERSION.rsplit(".", 1)[0],
            "sys_platform": self.sys_platform,
            "extra": extra,
        }


# Mirrors preflight_supported_runtime_platform in plugin/scripts/smart-install.sh.
TARGETS = (
    Target("linux-x86_64", "linux", "Linux", "x86_64", "posix"),
    Target("linux-aarch64", "linux", "Linux", "aarch64", "posix"),
    Target("macos-arm64", "darwin", "Darwin", "arm64", "posix"),
    Target("windows-x64", "win32", "Windows", "AMD64", "nt"),
)


def marker_applies(marker: str | None, env: dict[str, str]) -> bool:
    return marker is None or Marker(marker).evaluate(env)


def load_lock(lock_path: Path) -> dict[str, list[dict]]:
    """Group locked package entries by canonical name."""
    with lock_path.open("rb") as handle:
        data = tomllib.load(handle)
    packages: dict[str, list[dict]] = {}
    for entry in data.get("package", []):
        packages.setdefault(canonicalize_name(entry["name"]), []).append(entry)
    return packages


def select_entry(
    entries: list[dict], version: str | None, env: dict[str, str]
) -> dict | None:
    """Pick the locked entry an edge resolves to on this target."""
    if version is not None:
        entries = [entry for entry in entries if entry.get("version") == version]
    for entry in entries:
        markers = entry.get("resolution-markers")
        if not markers or any(marker_applies(marker, env) for marker in markers):
            return entry
    return entries[0] if len(entries) == 1 else None


def installed_versions(
    packages: dict[str, list[dict]], target: Target
) -> tuple[dict[str, str], dict[str, set[str]]]:
    """Walk the lock from claude-smart; return installed versions and extras."""
    env = target.environment()
    installed: dict[str, str] = {}
    extras: dict[str, set[str]] = {}
    pending: list[tuple[str, str | None, tuple[str, ...]]] = [(ROOT_PACKAGE, None, ())]
    seen: set[tuple[str, str | None, tuple[str, ...]]] = set()
    while pending:
        item = pending.pop()
        if item in seen:
            continue
        seen.add(item)
        name, version, requested_extras = item
        entry = select_entry(packages.get(name, []), version, env)
        if entry is None:
            continue
        installed[name] = entry["version"]
        extras.setdefault(name, set()).update(requested_extras)
        edges = list(entry.get("dependencies", []))
        for extra in requested_extras:
            edges.extend(entry.get("optional-dependencies", {}).get(extra, []))
        for edge in edges:
            if not marker_applies(edge.get("marker"), env):
                continue
            pending.append(
                (
                    canonicalize_name(edge["name"]),
                    edge.get("version"),
                    tuple(sorted(edge.get("extra", []))),
                )
            )
    return installed, extras


def vendored_requirements(vendor_path: Path) -> list[Requirement]:
    with (vendor_path / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle).get("project", {})
    return [Requirement(raw) for raw in project.get("dependencies", [])]


def find_problems(
    requirements: list[Requirement], packages: dict[str, list[dict]]
) -> list[str]:
    """Return one line per requirement the lock does not install on some target."""
    problems: dict[str, list[str]] = {}
    for target in TARGETS:
        installed, extras = installed_versions(packages, target)
        for requirement in requirements:
            if requirement.marker and not requirement.marker.evaluate(
                target.environment()
            ):
                continue
            name = canonicalize_name(requirement.name)
            locked = installed.get(name)
            if locked is None:
                reason = "not installed by plugin/uv.lock"
            elif not requirement.specifier.contains(Version(locked), prereleases=True):
                reason = f"plugin/uv.lock installs {locked}"
            elif missing := sorted(set(requirement.extras) - extras.get(name, set())):
                reason = (
                    f"plugin/uv.lock does not install extra(s) {', '.join(missing)}"
                )
            else:
                continue
            problems.setdefault(f"{requirement}: {reason}", []).append(target.label)
    return [f"{problem} [{', '.join(labels)}]" for problem, labels in problems.items()]


def check(vendor_path: Path, lock_path: Path) -> tuple[bool, str]:
    """Return (ok, message) for the vendored bundle at vendor_path."""
    if not (vendor_path / "pyproject.toml").is_file():
        return True, (
            f"OK: no vendored Reflexio at {vendor_path}; installs use the locked "
            "reflexio-ai, so there are no extra requirements to check."
        )
    requirements = vendored_requirements(vendor_path)
    problems = find_problems(requirements, load_lock(lock_path))
    if not problems:
        return True, (
            f"OK: plugin/uv.lock installs all {len(requirements)} runtime requirements "
            f"of the vendored Reflexio on {', '.join(t.label for t in TARGETS)}."
        )
    lines = [
        "error: the vendored Reflexio is installed with --no-deps, so these",
        "requirements would be missing or too old on user machines:",
        *(f"  - {problem}" for problem in problems),
        "Fix: add each requirement to plugin/pyproject.toml [project].dependencies,",
        "then regenerate the lock with `make relock`.",
    ]
    return False, "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify plugin/uv.lock installs the vendored Reflexio requirements."
    )
    parser.add_argument("--vendor-path", type=Path, default=DEFAULT_VENDOR)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    args = parser.parse_args(argv)
    ok, message = check(args.vendor_path, args.lock)
    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
