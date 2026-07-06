"""Tests for Reflexio dependency lock validation script."""

from __future__ import annotations

import json
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK_REFLEXIO_LOCK = REPO_ROOT / "scripts" / "check-reflexio-lock.py"


def _vendor_content_fields(vendor: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    file_count = 0
    for path in sorted(vendor.rglob("*"), key=lambda item: item.relative_to(vendor).as_posix()):
        relative = path.relative_to(vendor).as_posix()
        if relative == ".claude-smart-vendor.json" or not path.is_file():
            continue
        data = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
        file_count += 1
    return {
        "content_sha256": digest.hexdigest(),
        "content_file_count": file_count,
    }


def test_check_reflexio_lock_pypi_source_does_not_require_tomllib(
    tmp_path: Path,
) -> None:
    scripts = tmp_path / "scripts"
    plugin = tmp_path / "plugin"
    scripts.mkdir()
    plugin.mkdir()
    shutil.copy2(CHECK_REFLEXIO_LOCK, scripts / "check-reflexio-lock.py")
    (scripts / "tomllib.py").write_text(
        "raise ModuleNotFoundError('simulated Python 3.9')\n"
    )
    dependency = "reflexio-ai>=0.2.27"
    (plugin / "pyproject.toml").write_text(
        f'[project]\ndependencies = ["{dependency}"]\n'
    )
    (tmp_path / "reflexio.lock.json").write_text(
        json.dumps(
            {
                "package": "reflexio-ai",
                "repo": "https://github.com/ReflexioAI/reflexio.git",
                "version": "0.2.27",
                "commit": "a" * 40,
                "dependency": dependency,
                "source": "pypi",
                "updated_at": "2026-06-30T00:00:00Z",
            }
        )
    )

    result = subprocess.run(
        [sys.executable, str(scripts / "check-reflexio-lock.py")],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert f"OK: {dependency} (pypi)" in result.stdout


def test_check_reflexio_lock_vendor_source_validates_manifest_without_tomllib(
    tmp_path: Path,
) -> None:
    scripts = tmp_path / "scripts"
    plugin = tmp_path / "plugin"
    vendor = plugin / "vendor" / "reflexio"
    scripts.mkdir()
    plugin.mkdir()
    vendor.mkdir(parents=True)
    shutil.copy2(CHECK_REFLEXIO_LOCK, scripts / "check-reflexio-lock.py")
    (scripts / "tomllib.py").write_text(
        "raise ModuleNotFoundError('simulated Python 3.9')\n"
    )
    dependency = "reflexio-ai>=0.2.27"
    commit = "a" * 40
    (plugin / "pyproject.toml").write_text(
        f'[project]\ndependencies = ["{dependency}"]\n'
    )
    (vendor / "pyproject.toml").write_text(
        '[project]\nname = "reflexio-ai"\nversion = "0.2.27"\n'
    )
    (vendor / ".claude-smart-vendor.json").write_text(
        json.dumps(
            {
                "package": "reflexio-ai",
                "repo": "https://github.com/ReflexioAI/reflexio.git",
                "version": "0.2.27",
                "commit": commit,
                "dependency": dependency,
                **_vendor_content_fields(vendor),
            }
        )
    )
    (tmp_path / "reflexio.lock.json").write_text(
        json.dumps(
            {
                "package": "reflexio-ai",
                "repo": "https://github.com/ReflexioAI/reflexio.git",
                "version": "0.2.27",
                "commit": commit,
                "dependency": dependency,
                "source": "vendor",
                "vendor_path": "plugin/vendor/reflexio",
                "updated_at": "2026-06-30T00:00:00Z",
            }
        )
    )

    result = subprocess.run(
        [sys.executable, str(scripts / "check-reflexio-lock.py"), "--check-vendor"],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "OK: vendored Reflexio bundle present" in result.stdout
    assert f"OK: {dependency} (vendor)" in result.stdout


def test_check_reflexio_lock_rejects_stale_vendor_manifest(
    tmp_path: Path,
) -> None:
    scripts = tmp_path / "scripts"
    plugin = tmp_path / "plugin"
    vendor = plugin / "vendor" / "reflexio"
    scripts.mkdir()
    plugin.mkdir()
    vendor.mkdir(parents=True)
    shutil.copy2(CHECK_REFLEXIO_LOCK, scripts / "check-reflexio-lock.py")
    dependency = "reflexio-ai>=0.2.27"
    lock_commit = "a" * 40
    manifest_commit = "b" * 40
    (plugin / "pyproject.toml").write_text(
        f'[project]\ndependencies = ["{dependency}"]\n'
    )
    (vendor / "pyproject.toml").write_text(
        '[project]\nname = "reflexio-ai"\nversion = "0.2.27"\n'
    )
    (vendor / ".claude-smart-vendor.json").write_text(
        json.dumps(
            {
                "package": "reflexio-ai",
                "repo": "https://github.com/ReflexioAI/reflexio.git",
                "version": "0.2.27",
                "commit": manifest_commit,
                "dependency": dependency,
                **_vendor_content_fields(vendor),
            }
        )
    )
    (tmp_path / "reflexio.lock.json").write_text(
        json.dumps(
            {
                "package": "reflexio-ai",
                "repo": "https://github.com/ReflexioAI/reflexio.git",
                "version": "0.2.27",
                "commit": lock_commit,
                "dependency": dependency,
                "source": "vendor",
                "vendor_path": "plugin/vendor/reflexio",
                "updated_at": "2026-06-30T00:00:00Z",
            }
        )
    )

    result = subprocess.run(
        [sys.executable, str(scripts / "check-reflexio-lock.py"), "--check-vendor"],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 1
    assert "vendored Reflexio metadata mismatch" in result.stderr
    assert "commit" in result.stderr


def test_check_reflexio_lock_rejects_tampered_vendor_content(
    tmp_path: Path,
) -> None:
    scripts = tmp_path / "scripts"
    plugin = tmp_path / "plugin"
    vendor = plugin / "vendor" / "reflexio"
    scripts.mkdir()
    plugin.mkdir()
    vendor.mkdir(parents=True)
    shutil.copy2(CHECK_REFLEXIO_LOCK, scripts / "check-reflexio-lock.py")
    dependency = "reflexio-ai>=0.2.27"
    commit = "a" * 40
    (plugin / "pyproject.toml").write_text(
        f'[project]\ndependencies = ["{dependency}"]\n'
    )
    pyproject = vendor / "pyproject.toml"
    pyproject.write_text('[project]\nname = "reflexio-ai"\nversion = "0.2.27"\n')
    (vendor / ".claude-smart-vendor.json").write_text(
        json.dumps(
            {
                "package": "reflexio-ai",
                "repo": "https://github.com/ReflexioAI/reflexio.git",
                "version": "0.2.27",
                "commit": commit,
                "dependency": dependency,
                **_vendor_content_fields(vendor),
            }
        )
    )
    pyproject.write_text(
        '[project]\nname = "reflexio-ai"\nversion = "0.2.27"\n# stale edit\n'
    )
    (tmp_path / "reflexio.lock.json").write_text(
        json.dumps(
            {
                "package": "reflexio-ai",
                "repo": "https://github.com/ReflexioAI/reflexio.git",
                "version": "0.2.27",
                "commit": commit,
                "dependency": dependency,
                "source": "vendor",
                "vendor_path": "plugin/vendor/reflexio",
                "updated_at": "2026-06-30T00:00:00Z",
            }
        )
    )

    result = subprocess.run(
        [sys.executable, str(scripts / "check-reflexio-lock.py"), "--check-vendor"],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 1
    assert "vendored Reflexio content mismatch" in result.stderr
    assert "content_sha256" in result.stderr
