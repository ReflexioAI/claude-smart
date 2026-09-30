"""The bundled backend must keep opening ``reflexio.db``, and probes must ignore cwd.

claude-smart 0.3.0 bundled a Reflexio that resolves a null
``storage_config.db_path`` per org. When the shared ``reflexio.db`` held any
row another org had labelled, it refused to adopt the file and opened an
empty ``reflexio_claude-smart.db``, so existing installs looked wiped.
``backend-python-runner.py`` now pins the path before the backend starts.

These tests run the real runner against the vendored Reflexio, because only
the vendored copy has the resolver that caused the regression. CI vendors it
before running pytest (``.github/workflows/integration.yml``).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "plugin" / "scripts" / "backend-python-runner.py"
BACKEND_SERVICE = REPO_ROOT / "plugin" / "scripts" / "backend-service.sh"
VENDOR = REPO_ROOT / "plugin" / "vendor" / "reflexio"
ORG = "claude-smart"

requires_vendor = pytest.mark.skipif(
    not (VENDOR / "reflexio" / "server").is_dir(),
    reason="plugin/vendor/reflexio is a pack-time artifact; vendor it to run",
)


def _env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in ("REFLEXIO_URL", "REFLEXIO_ENV_FILE", "REFLEXIO_LOG_DIR"):
        env.pop(key, None)
    env.update(
        {
            "HOME": str(home),
            "LOCAL_STORAGE_PATH": str(home / "data"),
            "REFLEXIO_DEFAULT_ORG_ID": ORG,
            "OPENAI_API_KEY": "sk-placeholder-test",
            "PYTHONPATH": str(VENDOR),
        }
    )
    return env


def _config_file(home: Path) -> Path:
    return home / ".reflexio" / "configs" / f"config_{ORG}.json"


def _run_runner(home: Path, *metadata: str) -> subprocess.CompletedProcess[str]:
    # `--help` exercises the full runner path, pin included, without starting
    # services: reflexio.cli prints usage and exits 0.
    return subprocess.run(
        [sys.executable, "-P", str(RUNNER), *metadata, "--", "--help"],
        env=_env(home),
        cwd=home,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )


def _opened_db_path(home: Path) -> str:
    """Open storage the way Reflexio's configurator does and report the file."""
    script = (
        "from reflexio.server.services.configurator.local_file_config_storage "
        "import LocalFileConfigStorage\n"
        "from reflexio.server.services.storage.sqlite_storage import SQLiteStorage\n"
        f"config = LocalFileConfigStorage(org_id={ORG!r}).load_config()\n"
        f"storage = SQLiteStorage(org_id={ORG!r}, "
        "db_path=config.storage_config.db_path)\n"
        "print(storage.db_path)\n"
    )
    result = subprocess.run(
        [sys.executable, "-P", "-c", script],
        env=_env(home),
        cwd=home,
        text=True,
        capture_output=True,
        check=True,
        timeout=120,
    )
    return result.stdout.strip().splitlines()[-1]


def _write_legacy_db_with_foreign_label(home: Path) -> Path:
    """A 0.2.x-era shared file: claude-smart history plus another org's row."""
    legacy = home / "data" / "reflexio.db"
    legacy.parent.mkdir(parents=True)
    script = (
        "from reflexio.server.services.storage.sqlite_storage import SQLiteStorage\n"
        f"SQLiteStorage(org_id={ORG!r}, db_path={str(legacy)!r}).conn.close()\n"
    )
    subprocess.run(
        [sys.executable, "-P", "-c", script],
        env=_env(home),
        cwd=home,
        check=True,
        capture_output=True,
        timeout=120,
    )
    conn = sqlite3.connect(legacy)
    conn.execute("CREATE TABLE claude_smart_history (note TEXT)")
    conn.execute("INSERT INTO claude_smart_history VALUES ('remembered')")
    conn.execute(
        "INSERT INTO learning_jobs (org_id, user_id) VALUES ('e2e-other-org', 'u')"
    )
    conn.commit()
    conn.close()
    return legacy


def _write_config_with_null_db_path(home: Path) -> None:
    script = (
        "from reflexio.server.services.configurator.local_file_config_storage "
        "import LocalFileConfigStorage\n"
        f"LocalFileConfigStorage(org_id={ORG!r}).load_config()\n"
    )
    subprocess.run(
        [sys.executable, "-P", "-c", script],
        env=_env(home),
        cwd=home,
        check=True,
        capture_output=True,
        timeout=120,
    )
    assert json.loads(_config_file(home).read_text())["storage_config"] == {
        "db_path": None
    }


@requires_vendor
def test_unpinned_config_opens_an_empty_file_beside_a_foreign_labelled_db(
    tmp_path: Path,
) -> None:
    """The 0.3.0 regression, reproduced: without the pin, history is skipped."""
    legacy = _write_legacy_db_with_foreign_label(tmp_path)
    _write_config_with_null_db_path(tmp_path)

    assert _opened_db_path(tmp_path) != str(legacy)


@requires_vendor
def test_backend_runner_pins_reflexio_db_so_history_is_opened(tmp_path: Path) -> None:
    legacy = _write_legacy_db_with_foreign_label(tmp_path)
    _write_config_with_null_db_path(tmp_path)

    result = _run_runner(tmp_path, "--claude-smart-backend=1")

    assert result.returncode == 0, result.stderr
    assert json.loads(_config_file(tmp_path).read_text())["storage_config"] == {
        "db_path": str(legacy)
    }
    assert _opened_db_path(tmp_path) == str(legacy)
    conn = sqlite3.connect(legacy)
    assert conn.execute("SELECT note FROM claude_smart_history").fetchall() == [
        ("remembered",)
    ]
    conn.close()
    assert not (tmp_path / "data" / f"reflexio_{ORG}.db").exists()


@requires_vendor
def test_backend_runner_pins_a_fresh_install(tmp_path: Path) -> None:
    result = _run_runner(tmp_path, "--claude-smart-backend=1")

    assert result.returncode == 0, result.stderr
    config = json.loads(_config_file(tmp_path).read_text())
    assert config["storage_config"] == {
        "db_path": str(tmp_path / "data" / "reflexio.db")
    }


@requires_vendor
def test_backend_runner_keeps_a_custom_db_path(tmp_path: Path) -> None:
    _write_config_with_null_db_path(tmp_path)
    config_file = _config_file(tmp_path)
    config = json.loads(config_file.read_text())
    config["storage_config"] = {"db_path": str(tmp_path / "elsewhere.db")}
    config_file.write_text(json.dumps(config))
    before = config_file.read_bytes()

    result = _run_runner(tmp_path, "--claude-smart-backend=1")

    assert result.returncode == 0, result.stderr
    assert config_file.read_bytes() == before


@requires_vendor
def test_backend_runner_leaves_a_corrupt_config_untouched(tmp_path: Path) -> None:
    config_file = _config_file(tmp_path)
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{not json")

    result = _run_runner(tmp_path, "--claude-smart-backend=1")

    assert result.returncode == 0, result.stderr
    assert config_file.read_text() == "{not json"
    assert "could not pin SQLite db_path" in result.stderr


@requires_vendor
def test_runner_does_not_pin_outside_the_claude_smart_backend(tmp_path: Path) -> None:
    result = _run_runner(tmp_path)

    assert result.returncode == 0, result.stderr
    assert not _config_file(tmp_path).exists()


def test_vendor_import_preflight_ignores_a_reflexio_package_in_cwd(
    tmp_path: Path,
) -> None:
    """Launching Claude Code inside a reflexio checkout must not fail the preflight.

    ``python -`` puts the cwd first on ``sys.path``, so a ``reflexio/`` package
    there shadowed the bundled one and the backend never started.
    """
    vendor = tmp_path / "vendor"
    (vendor / "reflexio").mkdir(parents=True)
    (vendor / "reflexio" / "__init__.py").write_text("")
    checkout = tmp_path / "checkout"
    (checkout / "reflexio").mkdir(parents=True)
    (checkout / "reflexio" / "__init__.py").write_text("")

    service = BACKEND_SERVICE.read_text()
    start = service.index("verify_bundled_reflexio_import() {")
    end = service.index("\n}\n", start) + 3
    script = (
        f'VENDORED_REFLEXIO="{vendor}"\n'
        + service[start:end]
        + f'verify_bundled_reflexio_import "{sys.executable}" "{vendor}" "{vendor}"\n'
    )

    result = subprocess.run(
        ["bash", "-c", script],
        cwd=checkout,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


def _write_derived_db(home: Path) -> Path:
    """What 0.3.0 created when it declined, or had no, ``reflexio.db``."""
    derived = home / "data" / f"reflexio_{ORG}.db"
    derived.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(derived)
    conn.execute("CREATE TABLE written_by_030 (note TEXT)")
    conn.commit()
    conn.close()
    return derived


@requires_vendor
def test_install_that_began_on_030_keeps_its_per_org_file(tmp_path: Path) -> None:
    derived = _write_derived_db(tmp_path)
    _write_config_with_null_db_path(tmp_path)

    result = _run_runner(tmp_path, "--claude-smart-backend=1")

    assert result.returncode == 0, result.stderr
    assert _opened_db_path(tmp_path) == str(derived)
    assert not (tmp_path / "data" / "reflexio.db").exists()


@requires_vendor
def test_history_wins_over_030_file_and_the_other_file_is_named(
    tmp_path: Path,
) -> None:
    legacy = _write_legacy_db_with_foreign_label(tmp_path)
    derived = _write_derived_db(tmp_path)
    _write_config_with_null_db_path(tmp_path)

    result = _run_runner(tmp_path, "--claude-smart-backend=1")

    assert result.returncode == 0, result.stderr
    assert _opened_db_path(tmp_path) == str(legacy)
    assert str(derived) in result.stderr


@requires_vendor
def test_backend_runner_leaves_a_schema_invalid_config_untouched(
    tmp_path: Path,
) -> None:
    _write_config_with_null_db_path(tmp_path)
    config_file = _config_file(tmp_path)
    config = json.loads(config_file.read_text())
    config["storage_config"] = {"db_path": None, "not_a_field": True}
    config_file.write_text(json.dumps(config))
    before = config_file.read_bytes()

    result = _run_runner(tmp_path, "--claude-smart-backend=1")

    assert result.returncode == 0, result.stderr
    assert config_file.read_bytes() == before
    assert "could not pin SQLite db_path" in result.stderr


@requires_vendor
def test_pin_does_not_let_litellm_load_a_parent_dotenv(tmp_path: Path) -> None:
    """The pin imports LiteLLM before reflexio.cli installs its dotenv guard.

    LiteLLM's import-time ``load_dotenv()`` walks up to the first ``.env`` it
    finds, so the pin installs the guard itself. A ``-c`` main has no
    ``__file__``, so python-dotenv starts that walk at cwd -- one level below
    the planted file here.
    """
    (tmp_path / ".env").write_text("CS_LEAK_PROBE=leaked\n")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    script = (
        "import importlib.util, os\n"
        f"spec = importlib.util.spec_from_file_location('runner', {str(RUNNER)!r})\n"
        "runner = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(runner)\n"
        "os.environ['CLAUDE_SMART_BACKEND'] = '1'\n"
        "runner.pin_legacy_sqlite_db_path()\n"
        "print('probe=' + str(os.environ.get('CS_LEAK_PROBE')))\n"
    )

    result = subprocess.run(
        [sys.executable, "-P", "-c", script],
        env=_env(tmp_path / "home"),
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
        timeout=120,
    )

    assert "probe=None" in result.stdout, result.stdout + result.stderr
