"""Regressions from the 0.3.0 Reflexio vendor jump (0.2.28 -> 0.2.30).

The bundled backend must keep opening ``reflexio.db``, probes must ignore cwd,
and the local embedding daemon must actually start.

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
import socket
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
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env.update(
        {
            "HOME": str(home),
            "BACKEND_PORT": str(port),
            "LITELLM_LOCAL_MODEL_COST_MAP": "true",
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
    # Exercise the actual backend-start preparation. Intercept only the final
    # CLI delegation, so no daemon is launched by this identity regression.
    return _run_selected_command(
        home, ["services", "start", "--only", "backend"], metadata
    )


def _run_selected_command(home: Path, args: list[str], metadata: tuple[str, ...]):
    script = (
        "import runpy,sys\n"
        "runner = runpy.run_path(sys.argv[1])\n"
        "runner['main'].__globals__['runpy'].run_module = lambda *a, **k: None\n"
        "sys.argv = ['runner', *sys.argv[2:]]\n"
        "raise SystemExit(runner['main']())\n"
    )
    return subprocess.run(
        [sys.executable, "-P", "-c", script, str(RUNNER), *metadata, "--", *args],
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
    # A 0.2.x file carries the retired learning_jobs queue; newer reflexio
    # no longer creates it, so model the legacy table explicitly.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS learning_jobs "
        "(org_id TEXT NOT NULL, user_id TEXT NOT NULL)"
    )
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
        "db_path": None,
        "type": "sqlite",
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

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(_config_file(tmp_path).read_text())["storage_config"] == {
        "db_path": str(legacy),
        "type": "sqlite",
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

    assert result.returncode == 0, result.stdout + result.stderr
    config = json.loads(_config_file(tmp_path).read_text())
    assert config["storage_config"] == {
        "db_path": str(tmp_path / "data" / "reflexio.db"),
        "type": "sqlite",
    }


@requires_vendor
def test_backend_runner_keeps_a_custom_db_path(tmp_path: Path) -> None:
    _write_config_with_null_db_path(tmp_path)
    config_file = _config_file(tmp_path)
    config = json.loads(config_file.read_text())
    config["storage_config"] = {
        "type": "sqlite",
        "db_path": str(tmp_path / "elsewhere.db"),
    }
    config_file.write_text(json.dumps(config))
    before = config_file.read_bytes()

    result = _run_runner(tmp_path, "--claude-smart-backend=1")

    assert result.returncode == 0, result.stdout + result.stderr
    assert config_file.read_bytes() == before


@requires_vendor
def test_backend_runner_leaves_a_corrupt_config_untouched(tmp_path: Path) -> None:
    config_file = _config_file(tmp_path)
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{not json")

    result = _run_runner(tmp_path, "--claude-smart-backend=1")

    assert result.returncode != 0
    assert config_file.read_text() == "{not json"
    assert "Could not prepare the local configuration" in result.stderr


@requires_vendor
def test_runner_does_not_pin_outside_the_claude_smart_backend(tmp_path: Path) -> None:
    result = _run_runner(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
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

    assert result.returncode == 0, result.stdout + result.stderr


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

    assert result.returncode == 0, result.stdout + result.stderr
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

    assert result.returncode == 0, result.stdout + result.stderr
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

    assert result.returncode != 0
    assert config_file.read_bytes() == before
    assert "Could not prepare the local configuration" in result.stderr


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


@requires_vendor
def test_backend_env_makes_the_launcher_start_the_local_embedding_daemon(
    tmp_path: Path,
) -> None:
    """The plugin leaves default endpoint selection to the selected SDK."""
    service = BACKEND_SERVICE.read_text()
    assert "export REFLEXIO_EMBEDDING_SERVICE_URL=" not in service
    assert "export REFLEXIO_EMBEDDING_PROVIDER=" not in service
    assert "export CLAUDE_SMART_USE_LOCAL_EMBEDDING=" not in service
    probe = (
        "from reflexio.cli.run_services import should_start_local_embedding_service\n"
        "from reflexio.server.llm.providers.embedding_service_provider import (\n"
        "    inference_service_url,\n"
        ")\n"
        "print(should_start_local_embedding_service(), inference_service_url())\n"
    )
    script = "export EMBEDDING_PORT=18999\n" + f'"{sys.executable}" -P -c "$PROBE"\n'
    env = _env(tmp_path)
    for key in (
        "REFLEXIO_EMBEDDING_SERVICE_URL",
        "REFLEXIO_EMBEDDING_PROVIDER",
        "CLAUDE_SMART_USE_LOCAL_EMBEDDING",
    ):
        env.pop(key, None)
    env["PROBE"] = probe

    result = subprocess.run(
        ["bash", "-c", script],
        env=env,
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=True,
        timeout=120,
    )

    assert result.stdout.strip().splitlines()[-1] == ("True http://127.0.0.1:18999"), (
        result.stdout + result.stderr
    )


@requires_vendor
@pytest.mark.parametrize("derived", [False, True])
@pytest.mark.parametrize("historical_alias", [False, True])
def test_explicit_saved_config_upgrade_preserves_original_and_database(
    tmp_path, derived, historical_alias
):
    """Explicit conversion preserves old SQLite identity and original policy."""
    legacy = _write_legacy_db_with_foreign_label(tmp_path)
    if derived:
        target = legacy.with_name(f"reflexio_{ORG}.db")
        legacy.rename(target)
    else:
        target = legacy
    _write_config_with_null_db_path(tmp_path)
    original = _config_file(tmp_path)
    config = json.loads(original.read_text())
    config["storage_config"].pop("type")
    if historical_alias:
        config["storage_config"]["storage_type"] = "sqlite"
    original.write_text(json.dumps(config))
    before = original.read_bytes()
    # Plain untagged SQLite still loads; a retired field requires conversion.
    # Neither read path may quietly rewrite the saved document.
    rejected = subprocess.run(
        [
            sys.executable,
            "-P",
            "-c",
            f"from reflexio.server.services.configurator.local_file_config_storage import LocalFileConfigStorage; LocalFileConfigStorage(org_id={ORG!r}).load_config()",
        ],
        env=_env(tmp_path),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (rejected.returncode != 0) == historical_alias, rejected.stderr
    assert original.read_bytes() == before
    converted = original.with_suffix(".canonical.json")
    result = subprocess.run(
        [
            sys.executable,
            "-P",
            "-m",
            "reflexio.cli",
            "config",
            "upgrade-file",
            str(original),
            "--output",
            str(converted),
        ],
        env=_env(tmp_path),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert original.read_bytes() == before
    canonical = json.loads(converted.read_text())
    current_format = subprocess.run(
        [
            sys.executable,
            "-P",
            "-c",
            "from reflexio.config_file import FILE_FORMAT_VERSION; print(FILE_FORMAT_VERSION)",
        ],
        env=_env(tmp_path),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.strip()
    # The upgrade writes Reflexio's current file format, which Reflexio bumps
    # when a saved field changes, so compare against the bundled version.
    assert canonical.pop("_reflexio_config_file")["version"] == int(current_format)
    assert canonical["storage_config"] == {"type": "sqlite", "db_path": None}
    assert {
        key: value for key, value in canonical.items() if key != "storage_config"
    } == {
        key: value
        for key, value in config.items()
        if key not in {"storage_config", "_reflexio_config_file"}
    }
    backup = original.with_suffix(".pre-upgrade.json")
    original.rename(backup)
    converted.rename(original)
    result = _run_runner(tmp_path, "--claude-smart-backend=1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert backup.read_bytes() == before
    assert _opened_db_path(tmp_path) == str(target)
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT note FROM claude_smart_history").fetchall() == [
            ("remembered",)
        ]


@requires_vendor
@pytest.mark.parametrize("dataset", ["legacy", "derived", "custom"])
@pytest.mark.parametrize("adopt", [False, True])
def test_normal_runner_upgrades_agent_policy_before_pinning_owned_database(
    tmp_path, dataset, adopt
):
    """Normal launch preparation, not an offline conversion prerequisite."""
    target = _write_legacy_db_with_foreign_label(tmp_path)
    if dataset != "legacy":
        destination = target.with_name(
            f"reflexio_{ORG}.db" if dataset == "derived" else "operator-owned.db"
        )
        target.rename(destination)
        target = destination
    path = _config_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps(
        {
            # Untagged SQLite remains a supported current storage shape.
            "storage_config": {"db_path": str(target) if dataset == "custom" else None},
            "playbook_optimizer_config": {
                "enabled": True,
                "optimize_agent_playbooks": True,
                "auto_update_pending_agent_playbooks": adopt,
                "user_playbooks_enabled": True,
                "max_metric_calls": 7,
            },
            "opaque": {"preserve": "extension"},
        }
    ).encode()
    path.write_bytes(original)
    result = _run_runner(tmp_path, "--claude-smart-backend=1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "could not pin SQLite" not in result.stderr
    current = json.loads(path.read_text())
    assert current["storage_config"]["db_path"] == str(target)
    optimizer = current["playbook_optimizer_config"]
    assert optimizer["agent_playbooks_enabled"] is adopt
    assert optimizer["user_playbooks_enabled"] is True
    # reflexio-ai 0.2.34 converts the old enable flag and limit into one budget.
    assert optimizer["metric_call_budget"] == 7
    assert "max_metric_calls" not in optimizer
    assert "enabled" not in optimizer
    assert "optimize_agent_playbooks" not in optimizer
    assert "auto_update_pending_agent_playbooks" not in optimizer
    assert current["opaque"] == {"preserve": "extension"}
    backups = list((path.parent / ".config-migrations" / path.name).glob("*.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert _opened_db_path(tmp_path) == str(target)
    with sqlite3.connect(target) as connection:
        assert connection.execute(
            "SELECT note FROM claude_smart_history"
        ).fetchone() == ("remembered",)
    before = path.read_bytes()
    repeated = _run_runner(tmp_path, "--claude-smart-backend=1")
    assert repeated.returncode == 0, repeated.stderr
    assert path.read_bytes() == before


@requires_vendor
def test_forwarded_occupied_port_refuses_preparation_and_pin(tmp_path):
    path = _config_file(tmp_path)
    path.parent.mkdir(parents=True)
    original = b'{"storage_config":{"type":"sqlite","db_path":null},"playbook_optimizer_config":{"optimize_agent_playbooks":true,"auto_update_pending_agent_playbooks":false}}'
    path.write_bytes(original)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        result = subprocess.run(
            [
                sys.executable,
                "-P",
                str(RUNNER),
                "--claude-smart-backend=1",
                "--",
                "services",
                "start",
                "--only",
                "backend",
                "--backend-port",
                str(listener.getsockname()[1]),
            ],
            env=_env(tmp_path),
            cwd=tmp_path,
            text=True,
            capture_output=True,
            timeout=30,
        )
    assert result.returncode != 0
    assert "ports conflict" in result.stdout
    assert path.read_bytes() == original
    assert not (path.parent / ".config-migrations").exists()


@requires_vendor
def test_failed_identity_pin_never_delegates_to_cli_or_selects_another_database(
    tmp_path,
):
    legacy = _write_legacy_db_with_foreign_label(tmp_path)
    _write_config_with_null_db_path(tmp_path)
    original = _config_file(tmp_path).read_bytes()
    script = (
        "import importlib.util, sys\n"
        "from unittest.mock import patch\n"
        f"spec = importlib.util.spec_from_file_location('runner', {str(RUNNER)!r})\n"
        "runner = importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)\n"
        "sys.argv = ['runner', '--claude-smart-backend=1', '--', 'services', 'start', '--only', 'backend']\n"
        "with patch('reflexio.config_file.LocalConfigFile.edit_document', side_effect=OSError('private-marker')), patch.object(runner.runpy, 'run_module', side_effect=AssertionError('CLI must not run')):\n"
        "    runner.main()\n"
    )
    result = subprocess.run(
        [sys.executable, "-P", "-c", script],
        env=_env(tmp_path),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "startup stopped to preserve database identity" in result.stderr
    assert "private-marker" not in result.stderr
    assert "CLI must not run" not in result.stderr
    assert _config_file(tmp_path).read_bytes() == original
    assert not legacy.with_name(f"reflexio_{ORG}.db").exists()
    with sqlite3.connect(legacy) as connection:
        assert connection.execute(
            "SELECT note FROM claude_smart_history"
        ).fetchone() == ("remembered",)


@requires_vendor
@pytest.mark.parametrize(
    "args",
    [
        ["--help"],
        ["services", "stop"],
        ["services", "start", "--only", "docs"],
        ["services", "start", "--help"],
    ],
)
@pytest.mark.parametrize("saved", ["absent", "legacy", "corrupt"])
def test_non_backend_commands_never_prepare_or_pin_selected_configuration(
    tmp_path, args, saved
):
    path = _config_file(tmp_path)
    before = None
    if saved != "absent":
        path.parent.mkdir(parents=True)
        before = (
            b"{invalid-json"
            if saved == "corrupt"
            else (
                b'{"storage_config":{"type":"sqlite","db_path":null},'
                b'"playbook_optimizer_config":{"optimize_agent_playbooks":true}}'
            )
        )
        path.write_bytes(before)
    result = _run_selected_command(tmp_path, args, ("--claude-smart-backend=1",))
    assert result.returncode == 0, result.stdout + result.stderr
    assert path.exists() == (before is not None)
    if before is not None:
        assert path.read_bytes() == before
    assert not (path.parent / ".config-migrations").exists()
    assert not (tmp_path / "data").exists()


@requires_vendor
@pytest.mark.parametrize("only", [None, "backend,docs", "docs,backend"])
def test_default_and_combined_service_selection_still_pin_backend(tmp_path, only):
    _write_config_with_null_db_path(tmp_path)
    args = ["services", "start"]
    if only is not None:
        args.extend(["--only", only])
    result = _run_selected_command(tmp_path, args, ("--claude-smart-backend=1",))
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(_config_file(tmp_path).read_text())["storage_config"] == {
        "type": "sqlite",
        "db_path": str(tmp_path / "data" / "reflexio.db"),
    }
