"""Prepared-runtime repair works without SDK/backend imports or ambient Python paths."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import venv
from pathlib import Path

import filelock
import pytest
from claude_smart import env_config


@pytest.fixture(scope="module")
def owner_python(tmp_path_factory):
    root = tmp_path_factory.mktemp("prepared-settings-owner")
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(root)
    python = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    site = Path(
        subprocess.check_output(  # noqa: S603 — owned prepared interpreter and fixed argv.
            [
                str(python),
                "-I",
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ],
            text=True,
        ).strip()
    )
    # The publisher component deliberately needs only its package + filelock;
    # no SDK/backend/provider installation can block repairing its settings.
    package = Path(env_config.__file__).parent
    shutil.copytree(
        package, site / "claude_smart", ignore=shutil.ignore_patterns("__pycache__")
    )
    lock_package = Path(filelock.__file__).parent
    shutil.copytree(
        lock_package, site / "filelock", ignore=shutil.ignore_patterns("__pycache__")
    )
    metadata = lock_package.parent / f"filelock-{filelock.__version__}.dist-info"
    shutil.copytree(metadata, site / metadata.name)
    return python


def _run(owner_python, home, *arguments, extra=None):
    home.mkdir(exist_ok=True)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "SYSTEMROOT", "WINDIR", "TMP", "TEMP", "TMPDIR", "LANG"}
    }
    poison = home / "untrusted-python-path"
    (poison / "claude_smart").mkdir(parents=True, exist_ok=True)
    (poison / "claude_smart/__init__.py").write_text(
        "raise RuntimeError('Ambient Python path must not be imported')\n"
    )
    environment.update(
        HOME=str(home),
        USERPROFILE=str(home),
        PYTHONPATH=str(poison),
        REFLEXIO_ENV_FILE=str(home / "unrelated-sdk.env"),
    )
    environment.update(extra or {})
    return subprocess.run(  # noqa: S603 — owned prepared interpreter and fixed internal action.
        [str(owner_python), "-I", "-m", "claude_smart.env_config", *arguments],
        env=environment,
        text=True,
        capture_output=True,
        timeout=15,
    )


def test_real_entrypoint_repairs_missing_defaults_and_is_idempotent(
    owner_python, tmp_path
):
    result = _run(
        owner_python,
        tmp_path,
        "bootstrap-repair",
        extra={
            "REFLEXIO_ROW_LIMIT_INTERACTIONS": "9000",
            "CLAUDE_SMART_USE_LOCAL_CLI": "0",
        },
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ok"] is True
    selected = tmp_path / ".claude-smart/.env"
    assert "REFLEXIO_ROW_LIMIT_INTERACTIONS=9000\n" in selected.read_text()
    assert "CLAUDE_SMART_USE_LOCAL_CLI=0\n" in selected.read_text()
    original = selected.read_bytes()
    again = _run(owner_python, tmp_path, "bootstrap-repair")
    assert again.returncode == 0
    assert json.loads(again.stdout)["added"] == []
    assert selected.read_bytes() == original


@pytest.mark.parametrize("api_key", ["", "fake-secret"])
def test_remote_selection_never_creates_local_settings_even_without_key(
    owner_python, tmp_path, api_key
):
    result = _run(
        owner_python,
        tmp_path,
        "bootstrap-repair",
        extra={"REFLEXIO_URL": "https://remote.example/", "REFLEXIO_API_KEY": api_key},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["added"] == []
    assert not (tmp_path / ".claude-smart/.env").exists()
    assert "fake-secret" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "url", ["https://remote.example/", "http://localhost:8081/", ""]
)
def test_fresh_file_connection_and_empty_values_win_without_retargeting(
    owner_python, tmp_path, url
):
    selected = tmp_path / ".claude-smart/.env"
    selected.parent.mkdir()
    original = f'# selected\nREFLEXIO_URL="{url}"\nREFLEXIO_API_KEY=""\nREFLEXIO_USER_ID="shared"\nCLAUDE_SMART_HOST=opencode\nCLAUDE_SMART_READ_ONLY="1"\nREFLEXIO_ROW_LIMIT_INTERACTIONS=0\n'
    selected.write_text(original)
    result = _run(
        owner_python,
        tmp_path,
        "bootstrap-repair",
        extra={
            "REFLEXIO_URL": "https://stale-parent.example/",
            "REFLEXIO_API_KEY": "stale-secret",
            "REFLEXIO_ROW_LIMIT_INTERACTIONS": "9000",
        },
    )
    assert result.returncode == 0, result.stderr
    assert selected.read_text().startswith(original)
    assert not (selected.parent / ".env.bak").exists()
    assert "stale-secret" not in result.stdout + result.stderr
    if url.startswith("https:"):
        assert selected.read_text() == original
    else:
        assert "CLAUDE_SMART_USE_LOCAL_CLI=" in selected.read_text()


def test_busy_owner_is_transient_and_never_reports_success(owner_python, tmp_path):
    selected = tmp_path / ".claude-smart/.env"
    selected.parent.mkdir()
    native = filelock.WindowsFileLock if os.name == "nt" else filelock.UnixFileLock
    with native(
        str(selected) + ".lock",
        timeout=0,
        mode=0o600,
        fallback_to_soft=False,
        preserve_lock_file=True,
    ):
        result = _run(owner_python, tmp_path, "bootstrap-repair")
    assert result.returncode == 75
    assert not result.stdout
    assert "busy" in result.stderr
    assert not selected.exists()
    assert not (selected.parent / "install-failed").exists()


def test_unknown_internal_action_rejects_before_settings_creation(
    owner_python, tmp_path
):
    result = _run(owner_python, tmp_path, "unrecognized", "fake-secret")
    assert result.returncode == 2
    assert "Usage:" in result.stderr
    assert "fake-secret" not in result.stderr
    assert not (tmp_path / ".claude-smart").exists()


@pytest.mark.parametrize("separator", ["", "\u2028", "\v", "\r"])
def test_repair_does_not_treat_literal_value_suffix_as_connection(
    owner_python, tmp_path, separator
):
    selected = tmp_path / ".claude-smart/.env"
    selected.parent.mkdir()
    original = (
        "NOTE=literal" + separator + "REFLEXIO_URL=https://owned.example/\n"
    ).encode()
    selected.write_bytes(original)

    result = _run(owner_python, tmp_path, "bootstrap-repair")

    assert result.returncode == 0, result.stderr
    assert "CLAUDE_SMART_USE_LOCAL_CLI" in json.loads(result.stdout)["added"]
    assert selected.read_bytes().startswith(original)
    before = selected.read_bytes()
    retry = _run(owner_python, tmp_path, "bootstrap-repair")
    assert retry.returncode == 0, retry.stderr
    assert json.loads(retry.stdout)["added"] == []
    assert selected.read_bytes() == before


def test_repair_separates_additions_from_unterminated_literal_cr(
    owner_python, tmp_path
):
    selected = tmp_path / ".claude-smart/.env"
    selected.parent.mkdir()
    original = b"NOTE=literal\r"
    selected.write_bytes(original)

    result = _run(owner_python, tmp_path, "bootstrap-repair")

    assert result.returncode == 0, result.stderr
    assert selected.read_bytes().startswith(original + b"\n")
