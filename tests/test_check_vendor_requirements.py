"""Tests for scripts/check-vendor-requirements.py.

The vendored Reflexio is installed with ``--no-deps``, so plugin/uv.lock must
already install every requirement it declares. These tests prove the check
fails on a missing requirement, a too-low locked version, a missing extra and a
platform-only lock edge, and passes on the real repository state.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check-vendor-requirements.py"
REAL_LOCK = REPO_ROOT / "plugin" / "uv.lock"

_spec = importlib.util.spec_from_file_location("check_vendor_requirements", SCRIPT)
assert _spec and _spec.loader
check_vendor_requirements = importlib.util.module_from_spec(_spec)
# dataclasses resolves annotations through sys.modules, so register first.
sys.modules[_spec.name] = check_vendor_requirements
_spec.loader.exec_module(check_vendor_requirements)

LOCK = textwrap.dedent(
    """\
    version = 1
    requires-python = ">=3.12"

    [[package]]
    name = "claude-smart"
    version = "0.0.1"
    source = { editable = "." }
    dependencies = [
        { name = "reflexio-ai" },
        { name = "winonly", marker = "sys_platform == 'win32'" },
    ]

    [[package]]
    name = "reflexio-ai"
    version = "0.1.0"
    source = { registry = "https://pypi.org/simple" }
    dependencies = [
        { name = "litellm" },
        { name = "pydantic", extra = ["email"] },
        { name = "plain" },
    ]

    [[package]]
    name = "litellm"
    version = "1.103.1"
    source = { registry = "https://pypi.org/simple" }

    [[package]]
    name = "pydantic"
    version = "2.13.0"
    source = { registry = "https://pypi.org/simple" }

    [package.optional-dependencies]
    email = [{ name = "email-validator" }]

    [[package]]
    name = "email-validator"
    version = "2.0.0"
    source = { registry = "https://pypi.org/simple" }

    [[package]]
    name = "plain"
    version = "1.0.0"
    source = { registry = "https://pypi.org/simple" }

    [[package]]
    name = "winonly"
    version = "1.0.0"
    source = { registry = "https://pypi.org/simple" }
    """
)


def _vendor(tmp_path: Path, requirements: list[str]) -> Path:
    vendor = tmp_path / "vendor" / "reflexio"
    vendor.mkdir(parents=True)
    deps = ",\n".join(f'    "{req}"' for req in requirements)
    (vendor / "pyproject.toml").write_text(
        '[project]\nname = "reflexio-ai"\nversion = "0.1.1"\n'
        f"dependencies = [\n{deps}\n]\n"
    )
    return vendor


def _lock(tmp_path: Path) -> Path:
    lock = tmp_path / "uv.lock"
    lock.write_text(LOCK)
    return lock


def test_passes_when_lock_installs_every_requirement(tmp_path: Path) -> None:
    vendor = _vendor(
        tmp_path,
        [
            "litellm>=1.103.1,<2",
            "pydantic[email]>=2.0",
            "plain",
            "winonly; sys_platform == 'win32'",
        ],
    )
    ok, message = check_vendor_requirements.check(vendor, _lock(tmp_path))
    assert ok, message
    assert "all 4 runtime requirements" in message


def test_fails_on_requirement_missing_from_lock(tmp_path: Path) -> None:
    vendor = _vendor(tmp_path, ["litellm>=1.103.1", "brand-new-package>=0.3"])
    ok, message = check_vendor_requirements.check(vendor, _lock(tmp_path))
    assert not ok
    assert "brand-new-package>=0.3: not installed by plugin/uv.lock" in message
    assert "litellm" not in message
    assert "add each requirement to plugin/pyproject.toml" in message


def test_fails_when_floor_exceeds_locked_version(tmp_path: Path) -> None:
    vendor = _vendor(tmp_path, ["litellm>=1.200.0"])
    ok, message = check_vendor_requirements.check(vendor, _lock(tmp_path))
    assert not ok
    assert "litellm>=1.200.0: plugin/uv.lock installs 1.103.1" in message


def test_fails_when_requested_extra_is_not_locked(tmp_path: Path) -> None:
    vendor = _vendor(tmp_path, ["plain[speedups]"])
    ok, message = check_vendor_requirements.check(vendor, _lock(tmp_path))
    assert not ok
    assert "does not install extra(s) speedups" in message


def test_platform_only_lock_edge_does_not_cover_other_platforms(tmp_path: Path) -> None:
    vendor = _vendor(tmp_path, ["winonly"])
    ok, message = check_vendor_requirements.check(vendor, _lock(tmp_path))
    assert not ok
    failing_line = next(line for line in message.splitlines() if "winonly" in line)
    assert "linux-x86_64" in failing_line
    assert "macos-arm64" in failing_line
    assert "windows-x64" not in failing_line


def test_requirement_for_no_supported_target_is_ignored(tmp_path: Path) -> None:
    vendor = _vendor(tmp_path, ["legacy-shim; python_version < '3.10'"])
    ok, message = check_vendor_requirements.check(vendor, _lock(tmp_path))
    assert ok, message


def test_single_resolution_excluding_target_is_not_installed(tmp_path: Path) -> None:
    vendor = _vendor(tmp_path, ["plain"])
    lock = _lock(tmp_path)
    lock.write_text(
        LOCK.replace(
            'name = "plain"\nversion = "1.0.0"',
            'name = "plain"\nversion = "1.0.0"\n'
            "resolution-markers = [\"sys_platform == 'linux'\"]",
        )
    )
    ok, message = check_vendor_requirements.check(vendor, lock)
    assert not ok
    assert "macos-arm64" in message
    assert "windows-x64" in message
    assert "linux-x86_64" not in message


def test_passes_trivially_without_vendor(tmp_path: Path) -> None:
    ok, message = check_vendor_requirements.check(tmp_path / "missing", _lock(tmp_path))
    assert ok
    assert "no vendored Reflexio" in message


def test_real_lock_rejects_synthetic_gaps(tmp_path: Path) -> None:
    vendor = _vendor(tmp_path, ["not-a-locked-package-xyz>=1", "litellm>=99"])
    ok, message = check_vendor_requirements.check(vendor, REAL_LOCK)
    assert not ok
    assert "not-a-locked-package-xyz>=1: not installed by plugin/uv.lock" in message
    assert "litellm>=99: plugin/uv.lock installs" in message


def test_cli_passes_on_real_repository_state() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("OK:")


def test_cli_exits_nonzero_with_message_on_gap(tmp_path: Path) -> None:
    vendor = _vendor(tmp_path, ["brand-new-package"])
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--vendor-path",
            str(vendor),
            "--lock",
            str(_lock(tmp_path)),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 1
    assert "brand-new-package" in result.stderr
