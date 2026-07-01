"""Tests for Reflexio dependency lock validation script."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK_REFLEXIO_LOCK = REPO_ROOT / "scripts" / "check-reflexio-lock.py"


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


def test_check_reflexio_lock_vendor_source_does_not_require_tomllib(
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
    (plugin / "pyproject.toml").write_text(
        f'[project]\ndependencies = ["{dependency}"]\n'
    )
    (vendor / "pyproject.toml").write_text(
        '[project]\nname = "reflexio-ai"\nversion = "0.2.27"\n'
    )
    (tmp_path / "reflexio.lock.json").write_text(
        json.dumps(
            {
                "package": "reflexio-ai",
                "repo": "https://github.com/ReflexioAI/reflexio.git",
                "version": "0.2.27",
                "commit": "a" * 40,
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
