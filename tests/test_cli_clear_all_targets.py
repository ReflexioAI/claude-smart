"""clear-all target resolution: what is deleted, and every refusal path.

``clear-all`` is destructive, so these tests drive ``cmd_clear_all`` against
real temp trees and assert both what disappears and what must survive.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest  # type: ignore[reportMissingImports]
from claude_smart import cli


@pytest.fixture
def harness(monkeypatch, tmp_path):
    reflexio_dir = tmp_path / "reflexio"
    env_path = tmp_path / ".claude-smart" / ".env"
    config_path = reflexio_dir / "configs" / "config_claude-smart.json"
    plugin_root = tmp_path / "plugin"
    backend = plugin_root / "scripts" / "backend-service.sh"
    backend.parent.mkdir(parents=True)
    backend.write_text("#!/bin/sh\n")
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    monkeypatch.setattr(cli, "_REFLEXIO_DIR", reflexio_dir)
    monkeypatch.setattr(cli, "_CLAUDE_SMART_ENV_PATH", env_path)
    monkeypatch.setattr(cli, "_DEFAULT_STORAGE_ROOT", reflexio_dir / "data")
    monkeypatch.setattr(cli, "_REFLEXIO_CONFIG_PATH", config_path)
    monkeypatch.setattr(cli, "_PLUGIN_ROOT", plugin_root)
    monkeypatch.setattr(cli, "_BACKEND_SCRIPT", backend)
    monkeypatch.setenv("CLAUDE_SMART_STATE_DIR", str(sessions))
    monkeypatch.delenv("LOCAL_STORAGE_PATH", raising=False)

    statuses: list[str] = ["not running"]
    service_calls: list[str] = []
    service_rc: dict[str, int] = {}

    def fake_status(_script: Path, wait_ready_s: float = 3.0) -> str:  # noqa: ARG001
        return statuses.pop(0) if statuses else "not running"

    def fake_run_service(_script: Path, subcmd: str) -> int:
        service_calls.append(subcmd)
        return service_rc.get(subcmd, 0)

    monkeypatch.setattr(cli, "_service_status", fake_status)
    monkeypatch.setattr(cli, "_run_service", fake_run_service)

    def write_config(payload: Any) -> None:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            payload if isinstance(payload, str) else json.dumps(payload)
        )

    return argparse.Namespace(
        tmp=tmp_path,
        data=reflexio_dir / "data",
        env_path=env_path,
        sessions=sessions,
        statuses=statuses,
        service_calls=service_calls,
        service_rc=service_rc,
        write_config=write_config,
    )


def _clear(**kwargs: object) -> int:
    return cli.cmd_clear_all(argparse.Namespace(yes=True, **kwargs))


# --- dotenv + storage root ---------------------------------------------------


def test_read_dotenv_value_handles_comments_export_and_quotes(tmp_path) -> None:
    env = tmp_path / ".env"
    # The first real binding wins; comments, blank lines, look-alike keys,
    # "export" and surrounding quotes are all handled.
    env.write_text(
        "# LOCAL_STORAGE_PATH=/commented\n"
        "\n"
        "LOCAL_STORAGE_PATH_EXTRA=/wrong-key\n"
        'export LOCAL_STORAGE_PATH="/quoted/path"\n'
        "LOCAL_STORAGE_PATH=/second-binding-ignored\n"
    )
    assert cli._read_dotenv_value(env, "LOCAL_STORAGE_PATH") == "/quoted/path"
    assert cli._read_dotenv_value(env, "MISSING") is None
    assert cli._read_dotenv_value(tmp_path / "absent.env", "X") is None


def test_read_dotenv_value_keeps_unbalanced_quotes(tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text("KEY='/half\nOTHER=\"x'\n")

    assert cli._read_dotenv_value(env, "KEY") == "'/half"
    assert cli._read_dotenv_value(env, "OTHER") == "\"x'"


def test_storage_root_comes_from_dotenv_when_not_exported(harness) -> None:
    custom = harness.tmp / "custom-store"
    harness.env_path.parent.mkdir(parents=True)
    harness.env_path.write_text(f"export LOCAL_STORAGE_PATH='{custom}'\n")
    custom.mkdir()
    (custom / "row.db").write_text("x")
    harness.data.mkdir(parents=True)
    (harness.data / "default.db").write_text("keep")

    assert _clear() == 0

    assert not custom.exists()
    # The default root is NOT the configured one, so it is left alone.
    assert (harness.data / "default.db").read_text() == "keep"


def test_relative_storage_root_is_refused(harness, monkeypatch, capsys) -> None:
    monkeypatch.setenv("LOCAL_STORAGE_PATH", "relative/store")

    assert _clear() == 1

    assert "LOCAL_STORAGE_PATH must be an absolute path: relative/store" in (
        capsys.readouterr().err
    )
    assert harness.service_calls == []


# "/" is in the guarded set too, but is deliberately not exercised: a
# regression there would rmtree the real filesystem. Every case below lives
# under tmp_path, so a broken guard can only delete test data.
@pytest.mark.parametrize(
    "dangerous",
    ["home", "reflexio", "reflexio/configs", "plugin", "."],
)
def test_dangerous_storage_roots_are_refused(
    harness, monkeypatch, capsys, dangerous: str
) -> None:
    target = harness.tmp / dangerous  # "." is the plugin root's parent
    target.mkdir(parents=True, exist_ok=True)
    sentinel = target / "sentinel-file-that-must-survive"
    sentinel.write_text("keep")
    monkeypatch.setenv("LOCAL_STORAGE_PATH", str(target))

    assert _clear() == 1

    assert "refusing to delete dangerous path" in capsys.readouterr().err
    assert sentinel.read_text() == "keep"


def test_symlinked_storage_root_is_refused(harness, monkeypatch, capsys) -> None:
    real = harness.tmp / "real-data"
    real.mkdir()
    (real / "row.db").write_text("keep")
    link = harness.tmp / "data-link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("LOCAL_STORAGE_PATH", str(link))

    assert _clear() == 1

    assert f"refusing to delete symlink target: {link}" in capsys.readouterr().err
    assert (real / "row.db").read_text() == "keep"


# --- reflexio config shapes --------------------------------------------------


@pytest.mark.parametrize(
    ("storage_config", "kind"),
    [
        ({"managed_by": "platform", "db_path": "/x"}, "remote"),
        ({"type": "Supabase"}, "remote"),
        ({"storage_type": "postgres"}, "remote"),
        ({"type": "disk"}, "disk"),
        ({"type": "sqlite"}, "sqlite"),
        ({"url": "u", "key": "k", "db_url": "d"}, "remote"),
        ({"db_url": "postgres://x"}, "remote"),
        ({"dir_path": "/d"}, "disk"),
        ({"db_path": "/d.db"}, "sqlite"),
        ({}, "sqlite"),
    ],
)
def test_storage_config_kind_classifies_known_shapes(
    storage_config: dict[str, object], kind: str
) -> None:
    assert cli._storage_config_kind(storage_config) == kind


def test_storage_config_kind_refuses_unknown_shapes() -> None:
    with pytest.raises(cli._ClearAllError, match="unsupported reflexio storage_config"):
        cli._storage_config_kind({"bucket": "s3://somewhere"})


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("{not json", "could not read reflexio config"),
        ("[1, 2]", "is not a JSON object"),
        (
            {"storage_config": ["sqlite"]},
            "reflexio storage_config is not a JSON object",
        ),
        ({"storage_config": {"type": "postgres"}}, "remote Supabase/Postgres storage"),
        (
            {"storage_config": {"type": "disk"}},
            "configured disk storage is missing dir_path",
        ),
        (
            {"storage_config": {"db_path": "relative.db"}},
            "configured SQLite db_path must be an absolute path",
        ),
    ],
)
def test_unusable_reflexio_config_aborts_before_touching_anything(
    harness, capsys, payload: object, message: str
) -> None:
    harness.data.mkdir(parents=True)
    (harness.data / "row.db").write_text("keep")
    harness.write_config(payload)

    assert _clear() == 1

    assert message in capsys.readouterr().err
    assert (harness.data / "row.db").read_text() == "keep"
    assert harness.service_calls == []


def test_sqlite_config_removes_database_and_its_sidecars(harness, capsys) -> None:
    db = harness.tmp / "elsewhere" / "reflexio.db"
    db.parent.mkdir()
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db}{suffix}").write_text("data")
    unrelated = db.parent / "notes.txt"
    unrelated.write_text("keep")
    harness.write_config({"storage_config": {"db_path": f"  {db}  "}})

    assert _clear() == 0

    assert sorted(p.name for p in db.parent.iterdir()) == ["notes.txt"]
    assert "removed 3 data target(s)" in capsys.readouterr().out


def test_sqlite_config_without_db_path_only_clears_storage_root(harness) -> None:
    harness.data.mkdir(parents=True)
    harness.write_config({"storage_config": {"type": "sqlite", "db_path": "  "}})

    assert _clear() == 0
    assert not harness.data.exists()


def test_disk_config_removes_only_org_directories(harness) -> None:
    base = harness.tmp / "disk-store"
    (base / "disk_org-a").mkdir(parents=True)
    (base / "disk_org-b").mkdir()
    (base / "other-dir").mkdir()
    harness.write_config({"storage_config": {"dir_path": str(base)}})

    assert _clear() == 0

    assert sorted(p.name for p in base.iterdir()) == ["other-dir"]


def test_disk_config_pointing_at_missing_dir_is_a_noop(harness, capsys) -> None:
    harness.write_config({"storage_config": {"dir_path": str(harness.tmp / "nope")}})

    assert _clear() == 0
    assert "nothing to wipe" in capsys.readouterr().out


def test_disk_config_refuses_symlink_and_file_paths(harness, capsys) -> None:
    real = harness.tmp / "real-disk"
    (real / "disk_org").mkdir(parents=True)
    link = harness.tmp / "disk-link"
    link.symlink_to(real, target_is_directory=True)
    harness.write_config({"storage_config": {"dir_path": str(link)}})

    assert _clear() == 1
    assert "refusing to inspect symlink disk storage dir" in capsys.readouterr().err
    assert (real / "disk_org").is_dir()

    as_file = harness.tmp / "disk-file"
    as_file.write_text("")
    harness.write_config({"storage_config": {"dir_path": str(as_file)}})

    assert _clear() == 1
    assert "configured disk storage path is not a directory" in capsys.readouterr().err


def test_duplicate_targets_are_removed_once(harness, monkeypatch, capsys) -> None:
    store = harness.tmp / "store"
    (store / "disk_org").mkdir(parents=True)
    monkeypatch.setenv("LOCAL_STORAGE_PATH", str(store / "disk_org"))
    harness.write_config({"storage_config": {"dir_path": str(store)}})

    assert _clear() == 0

    assert "removed 1 data target(s)" in capsys.readouterr().out
    assert list(store.iterdir()) == []


def test_kind_mismatch_during_removal_reports_error(harness, capsys) -> None:
    db = harness.tmp / "db-is-a-dir.db"
    db.mkdir()
    harness.write_config({"storage_config": {"db_path": str(db)}})

    assert _clear() == 1

    assert "expected file target but found non-file" in capsys.readouterr().err
    assert db.is_dir()


def test_remove_target_rejects_mismatched_and_unknown_kinds(tmp_path) -> None:
    file_path = tmp_path / "f"
    file_path.write_text("")
    Target = cli._ClearAllTarget

    with pytest.raises(cli._ClearAllError, match="expected directory target"):
        cli._remove_clear_all_target(Target(file_path, "dir", "x"))
    with pytest.raises(cli._ClearAllError, match="unknown clear-all target kind"):
        cli._remove_clear_all_target(Target(file_path, "socket", "x"))
    assert not cli._remove_clear_all_target(Target(tmp_path / "gone", "file", "x"))
    assert file_path.exists()


# --- service lifecycle around the wipe ---------------------------------------


def test_restart_failure_after_wipe_is_reported(harness, capsys) -> None:
    harness.statuses[:] = ["running on http://localhost:8071", "not running"]
    harness.service_rc["start"] = 5
    harness.data.mkdir(parents=True)

    assert _clear() == 5

    assert harness.service_calls == ["stop", "start"]
    assert "reflexio backend failed to start (exit 5)" in capsys.readouterr().err
    assert not harness.data.exists()


def test_backend_not_reporting_running_after_restart_fails(harness, capsys) -> None:
    harness.statuses[:] = [
        "running on http://localhost:8071",
        "not running",
        "starting",
    ]

    assert _clear() == 1

    captured = capsys.readouterr()
    assert "reflexio backend did not report running: starting" in captured.err
    assert "reflexio backend: starting" in captured.out


def test_unremovable_session_buffer_is_a_warning_not_a_failure(harness, capsys) -> None:
    harness.sessions.mkdir()
    (harness.sessions / "ok.jsonl").write_text("{}\n")
    (harness.sessions / "stuck.jsonl").mkdir()  # unlink() on a dir fails

    assert _clear() == 0

    captured = capsys.readouterr()
    assert "warning: could not remove" in captured.err
    assert "stuck.jsonl" in captured.err
    assert "Removed 1 local session buffer(s)." in captured.out
    assert not (harness.sessions / "ok.jsonl").exists()
