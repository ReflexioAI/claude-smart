"""Tests for ``claude_smart.cli.cmd_clear_all``."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from claude_smart import cli


def _args(*, yes: bool) -> argparse.Namespace:
    return argparse.Namespace(yes=yes)


@pytest.fixture
def clear_all_harness(monkeypatch, tmp_path):
    """Isolate clear-all paths and service calls from the real user machine."""
    reflexio_dir = tmp_path / "reflexio"
    env_path = reflexio_dir / ".env"
    config_path = reflexio_dir / "configs" / "config_self-host-org.json"
    plugin_root = tmp_path / "plugin"
    backend_script = plugin_root / "scripts" / "backend-service.sh"
    backend_script.parent.mkdir(parents=True)
    backend_script.write_text("#!/bin/sh\n")
    plugin_root.mkdir(exist_ok=True)

    monkeypatch.setattr(cli, "_REFLEXIO_DIR", reflexio_dir)
    monkeypatch.setattr(cli, "_REFLEXIO_ENV_PATH", env_path)
    monkeypatch.setattr(cli, "_DEFAULT_STORAGE_ROOT", reflexio_dir / "data")
    monkeypatch.setattr(cli, "_REFLEXIO_CONFIG_PATH", config_path)
    monkeypatch.setattr(cli, "_PLUGIN_ROOT", plugin_root)
    monkeypatch.setattr(cli, "_BACKEND_SCRIPT", backend_script)
    monkeypatch.setenv("CLAUDE_SMART_STATE_DIR", str(tmp_path / "sessions"))
    monkeypatch.delenv("LOCAL_STORAGE_PATH", raising=False)

    calls: list[tuple[str, str]] = []
    statuses: list[str] = ["not running"]

    def fake_status(_script: Path, wait_ready_s: float = 3.0) -> str:  # noqa: ARG001
        return statuses.pop(0) if statuses else "not running"

    def fake_run_service(script: Path, subcmd: str) -> int:
        calls.append((script.name, subcmd))
        return 0

    monkeypatch.setattr(cli, "_service_status", fake_status)
    monkeypatch.setattr(cli, "_run_service", fake_run_service)

    def write_config(storage_config: dict[str, Any]) -> None:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps({"storage_config": storage_config}))

    return {
        "tmp": tmp_path,
        "reflexio_dir": reflexio_dir,
        "env_path": env_path,
        "config_path": config_path,
        "storage_root": reflexio_dir / "data",
        "state_dir": tmp_path / "sessions",
        "calls": calls,
        "statuses": statuses,
        "write_config": write_config,
    }


def test_clear_all_without_yes_is_noop(clear_all_harness, capsys) -> None:
    root = clear_all_harness["storage_root"]
    root.mkdir(parents=True)
    (root / "reflexio.db").write_text("db")
    state_dir = clear_all_harness["state_dir"]
    state_dir.mkdir()
    (state_dir / "session.jsonl").write_text("{}\n")

    rc = cli.cmd_clear_all(_args(yes=False))

    assert rc == 1
    assert root.exists()
    assert (state_dir / "session.jsonl").exists()
    assert clear_all_harness["calls"] == []
    assert "Re-run with --yes" in capsys.readouterr().out


def test_clear_all_deletes_default_storage_root_from_env(
    clear_all_harness, monkeypatch
) -> None:
    root = clear_all_harness["tmp"] / "custom-data"
    root.mkdir()
    (root / "reflexio.db").write_text("db")
    state_dir = clear_all_harness["state_dir"]
    state_dir.mkdir()
    (state_dir / "session.jsonl").write_text("{}\n")
    (state_dir / "keep.txt").write_text("keep")
    monkeypatch.setenv("LOCAL_STORAGE_PATH", str(root))

    rc = cli.cmd_clear_all(_args(yes=True))

    assert rc == 0
    assert not root.exists()
    assert not (state_dir / "session.jsonl").exists()
    assert (state_dir / "keep.txt").exists()


def test_clear_all_reads_default_storage_root_from_reflexio_env(
    clear_all_harness,
) -> None:
    root = clear_all_harness["tmp"] / "env-data"
    root.mkdir()
    (root / "reflexio.db").write_text("db")
    env_path = clear_all_harness["env_path"]
    env_path.parent.mkdir(parents=True)
    env_path.write_text(f'LOCAL_STORAGE_PATH="{root}"\n')

    rc = cli.cmd_clear_all(_args(yes=True))

    assert rc == 0
    assert not root.exists()


def test_clear_all_missing_storage_root_is_success(clear_all_harness, capsys) -> None:
    rc = cli.cmd_clear_all(_args(yes=True))

    assert rc == 0
    assert "nothing to wipe" in capsys.readouterr().out


def test_clear_all_stops_and_restarts_running_backend(clear_all_harness) -> None:
    root = clear_all_harness["storage_root"]
    root.mkdir(parents=True)
    (root / "reflexio.db").write_text("db")
    clear_all_harness["statuses"][:] = [
        "running on http://localhost:8071",
        "not running",
        "running on http://localhost:8071",
    ]

    rc = cli.cmd_clear_all(_args(yes=True))

    assert rc == 0
    assert clear_all_harness["calls"] == [
        ("backend-service.sh", "stop"),
        ("backend-service.sh", "start"),
    ]
    assert not root.exists()


def test_clear_all_aborts_when_backend_stop_fails(
    clear_all_harness, monkeypatch, capsys
) -> None:
    root = clear_all_harness["storage_root"]
    root.mkdir(parents=True)
    clear_all_harness["statuses"][:] = ["running on http://localhost:8071"]

    def fake_run_service(_script: Path, subcmd: str) -> int:
        clear_all_harness["calls"].append(("backend-service.sh", subcmd))
        return 7 if subcmd == "stop" else 0

    monkeypatch.setattr(cli, "_run_service", fake_run_service)

    rc = cli.cmd_clear_all(_args(yes=True))

    assert rc == 7
    assert root.exists()
    assert clear_all_harness["calls"] == [("backend-service.sh", "stop")]
    assert "failed to stop" in capsys.readouterr().err


def test_clear_all_aborts_when_backend_still_running(clear_all_harness, capsys) -> None:
    root = clear_all_harness["storage_root"]
    root.mkdir(parents=True)
    clear_all_harness["statuses"][:] = [
        "running on http://localhost:8071",
        "running on http://localhost:8071",
    ]

    rc = cli.cmd_clear_all(_args(yes=True))

    assert rc == 1
    assert root.exists()
    assert clear_all_harness["calls"] == [("backend-service.sh", "stop")]
    assert "still running" in capsys.readouterr().err


def test_clear_all_reports_start_failure_after_wipe(
    clear_all_harness, monkeypatch, capsys
) -> None:
    root = clear_all_harness["storage_root"]
    root.mkdir(parents=True)
    clear_all_harness["statuses"][:] = [
        "running on http://localhost:8071",
        "not running",
        "not running",
    ]

    def fake_run_service(_script: Path, subcmd: str) -> int:
        clear_all_harness["calls"].append(("backend-service.sh", subcmd))
        return 9 if subcmd == "start" else 0

    monkeypatch.setattr(cli, "_run_service", fake_run_service)

    rc = cli.cmd_clear_all(_args(yes=True))

    assert rc == 9
    assert not root.exists()
    assert "failed to start" in capsys.readouterr().err


def test_clear_all_deletes_custom_sqlite_sidecars(clear_all_harness) -> None:
    root = clear_all_harness["storage_root"]
    root.mkdir(parents=True)
    db_path = clear_all_harness["tmp"] / "sqlite" / "custom.db"
    db_path.parent.mkdir()
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(f"{db_path}{suffix}").write_text("db")
    clear_all_harness["write_config"]({"db_path": str(db_path)})

    rc = cli.cmd_clear_all(_args(yes=True))

    assert rc == 0
    assert not root.exists()
    assert db_path.parent.exists()
    for suffix in ("", "-wal", "-shm", "-journal"):
        assert not Path(f"{db_path}{suffix}").exists()


def test_clear_all_deletes_custom_disk_org_dirs_only(clear_all_harness) -> None:
    root = clear_all_harness["storage_root"]
    root.mkdir(parents=True)
    disk_base = clear_all_harness["tmp"] / "disk-store"
    disk_org = disk_base / "disk_self-host-org"
    other = disk_base / "not-reflexio"
    disk_org.mkdir(parents=True)
    other.mkdir()
    (disk_org / "entity.md").write_text("x")
    (other / "keep.txt").write_text("keep")
    clear_all_harness["write_config"]({"dir_path": str(disk_base)})

    rc = cli.cmd_clear_all(_args(yes=True))

    assert rc == 0
    assert not root.exists()
    assert disk_base.exists()
    assert not disk_org.exists()
    assert other.exists()
    assert clear_all_harness["config_path"].exists()


def test_clear_all_refuses_remote_storage_config(clear_all_harness, capsys) -> None:
    root = clear_all_harness["storage_root"]
    root.mkdir(parents=True)
    state_dir = clear_all_harness["state_dir"]
    state_dir.mkdir()
    (state_dir / "session.jsonl").write_text("{}\n")
    clear_all_harness["write_config"](
        {"type": "postgres", "db_url": "postgres://example/db"}
    )

    rc = cli.cmd_clear_all(_args(yes=True))

    assert rc == 1
    assert root.exists()
    assert (state_dir / "session.jsonl").exists()
    assert clear_all_harness["calls"] == []
    assert (
        "remote Supabase/Postgres storage is not supported" in capsys.readouterr().err
    )


def test_clear_all_refuses_dangerous_storage_root(
    clear_all_harness, monkeypatch, capsys
) -> None:
    reflexio_dir = clear_all_harness["reflexio_dir"]
    reflexio_dir.mkdir(parents=True)
    monkeypatch.setenv("LOCAL_STORAGE_PATH", str(reflexio_dir))

    rc = cli.cmd_clear_all(_args(yes=True))

    assert rc == 1
    assert reflexio_dir.exists()
    assert "refusing to delete dangerous path" in capsys.readouterr().err


def test_clear_all_refuses_symlink_storage_root(
    clear_all_harness, monkeypatch, tmp_path, capsys
) -> None:
    real_root = tmp_path / "real-data"
    real_root.mkdir()
    link_root = tmp_path / "linked-data"
    link_root.symlink_to(real_root, target_is_directory=True)
    monkeypatch.setenv("LOCAL_STORAGE_PATH", str(link_root))

    rc = cli.cmd_clear_all(_args(yes=True))

    assert rc == 1
    assert real_root.exists()
    assert "refusing to delete symlink target" in capsys.readouterr().err
