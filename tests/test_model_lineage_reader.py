"""Behavioral tests for dashboard model provenance SQLite reader."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
READER = REPO_ROOT / "plugin" / "dashboard" / "lib" / "model-lineage.ts"
SOURCE_GUARD = REPO_ROOT / "plugin" / "dashboard" / "lib" / "model-provenance-source.ts"
NODE = shutil.which("node")


def _require_node() -> str:
    if not NODE:
        raise AssertionError("node is required to exercise model-lineage.ts")
    return NODE


def _run_node(script: str, env: dict[str, str] | None = None):
    node = _require_node()
    proc = subprocess.run(
        [node, "--input-type=module", "--eval", script],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={**(os.environ if env is None else env), "NODE_NO_WARNINGS": "1"},
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    # node may still emit experimental warnings on stderr; stdout must be pure JSON
    return json.loads(proc.stdout)


def _run_reader(db_path: Path | None, entity_type: str, entity_id: str) -> dict:
    sqlite_path = (
        str(db_path)
        if db_path is not None
        else "/tmp/definitely-missing-reflexio-model-lineage.db"
    )
    script = f"""
import {{ getLearningModelProvenance }} from {json.dumps(READER.as_uri())};
const result = getLearningModelProvenance(
  {json.dumps(entity_type)},
  {json.dumps(entity_id)},
  {json.dumps(sqlite_path)},
);
process.stdout.write(JSON.stringify(result));
"""
    return _run_node(script)


def _local_env(home: Path, **extra: str) -> dict[str, str]:
    """An isolated HOME with no inherited Reflexio path overrides."""
    env = dict(os.environ)
    for key in ("REFLEXIO_LOG_DIR", "LOCAL_STORAGE_PATH"):
        env.pop(key, None)
    env.update(HOME=str(home), USERPROFILE=str(home), **extra)
    return env


def _resolve_db_path(env: dict[str, str]) -> str | None:
    script = f"""
import {{ resolveLocalReflexioDbPath }} from {json.dumps(READER.as_uri())};
process.stdout.write(JSON.stringify(resolveLocalReflexioDbPath()));
"""
    return _run_node(script, env)


def _write_org_config(home: Path, storage_config: dict | None) -> None:
    configs = home / ".reflexio" / "configs"
    configs.mkdir(parents=True, exist_ok=True)
    body = {} if storage_config is None else {"storage_config": storage_config}
    (configs / "config_claude-smart.json").write_text(json.dumps(body))


def _create_db(path: Path, *, with_model_cols: bool = True) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    if with_model_cols:
        conn.execute(
            """
            CREATE TABLE lineage_event (
              event_id INTEGER PRIMARY KEY,
              org_id TEXT NOT NULL,
              entity_type TEXT NOT NULL,
              entity_id TEXT NOT NULL,
              op TEXT NOT NULL,
              created_at INTEGER NOT NULL,
              model_name TEXT,
              provider TEXT
            )
            """
        )
    else:
        conn.execute(
            """
            CREATE TABLE lineage_event (
              event_id INTEGER PRIMARY KEY,
              org_id TEXT NOT NULL,
              entity_type TEXT NOT NULL,
              entity_id TEXT NOT NULL,
              op TEXT NOT NULL,
              created_at INTEGER NOT NULL
            )
            """
        )
    return conn


def test_reader_returns_observed_model_for_profile(tmp_path: Path) -> None:
    db = tmp_path / "reflexio.db"
    conn = _create_db(db)
    conn.execute(
        """
        INSERT INTO lineage_event
          (event_id, org_id, entity_type, entity_id, op, created_at, model_name, provider)
        VALUES
          (1, 'org', 'profile', 'pref-1', 'create', 100, 'MiniMax-M3', 'minimax')
        """
    )
    conn.commit()
    conn.close()

    result = _run_reader(db, "profile", "pref-1")
    assert result["unavailable"] is False
    assert result["modelName"] == "MiniMax-M3"
    assert result["provider"] == "minimax"
    assert result["entityType"] == "profile"
    assert result["entityId"] == "pref-1"


def test_reader_prefers_row_with_observed_model_over_newer_empty_row(
    tmp_path: Path,
) -> None:
    db = tmp_path / "reflexio.db"
    conn = _create_db(db)
    conn.executemany(
        """
        INSERT INTO lineage_event
          (event_id, org_id, entity_type, entity_id, op, created_at, model_name, provider)
        VALUES (?, 'org', 'user_playbook', '101', ?, ?, ?, ?)
        """,
        [
            (1, "create", 100, "MiniMax-M3", "minimax"),
            (2, "status_change", 200, None, None),  # newer, but no model
        ],
    )
    conn.commit()
    conn.close()

    result = _run_reader(db, "user_playbook", "101")
    assert result["unavailable"] is False
    assert result["modelName"] == "MiniMax-M3"
    assert result["provider"] == "minimax"


def test_reader_marks_historical_row_without_model_as_not_recorded(
    tmp_path: Path,
) -> None:
    db = tmp_path / "reflexio.db"
    conn = _create_db(db)
    conn.execute(
        """
        INSERT INTO lineage_event
          (event_id, org_id, entity_type, entity_id, op, created_at, model_name, provider)
        VALUES
          (1, 'org', 'profile', 'old-pref', 'create', 100, NULL, NULL)
        """
    )
    conn.commit()
    conn.close()

    result = _run_reader(db, "profile", "old-pref")
    assert result["unavailable"] is False
    assert result["modelName"] is None
    assert result["provider"] is None
    assert "not recorded" in (result.get("reason") or "").lower()


def test_reader_supports_schema_without_model_columns(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    conn = _create_db(db, with_model_cols=False)
    conn.execute(
        """
        INSERT INTO lineage_event
          (event_id, org_id, entity_type, entity_id, op, created_at)
        VALUES
          (1, 'org', 'agent_playbook', '202', 'create', 100)
        """
    )
    conn.commit()
    conn.close()

    result = _run_reader(db, "agent_playbook", "202")
    assert result["unavailable"] is False
    assert result["modelName"] is None
    assert result["provider"] is None


def test_reader_missing_entity_is_not_unavailable(tmp_path: Path) -> None:
    db = tmp_path / "reflexio.db"
    conn = _create_db(db)
    conn.commit()
    conn.close()

    result = _run_reader(db, "profile", "missing-id")
    assert result["unavailable"] is False
    assert result["modelName"] is None
    assert result["provider"] is None
    assert "no lineage events" in (result.get("reason") or "").lower()


def test_reader_missing_database_is_unavailable() -> None:
    result = _run_reader(None, "profile", "pref-1")
    assert result["unavailable"] is True
    assert result["reason"] == "database unavailable"


def test_reader_missing_lineage_table_is_unavailable(tmp_path: Path) -> None:
    db = tmp_path / "reflexio.db"
    sqlite3.connect(db).close()

    result = _run_reader(db, "profile", "pref-1")
    assert result["unavailable"] is True
    assert result["reason"] == "lineage_event table not present"


def test_resolver_uses_db_path_pinned_in_org_config(tmp_path: Path) -> None:
    pinned = tmp_path / "elsewhere" / "reflexio_claude-smart.db"
    _write_org_config(tmp_path, {"db_path": str(pinned)})
    # A legacy file exists too; the pinned path must still win.
    data = tmp_path / ".reflexio" / "data"
    data.mkdir(parents=True)
    (data / "reflexio.db").touch()

    assert _resolve_db_path(_local_env(tmp_path)) == str(pinned)


def test_resolver_honors_reflexio_log_dir_for_config_location(
    tmp_path: Path,
) -> None:
    base = tmp_path / "custom-base"
    pinned = tmp_path / "pinned.db"
    _write_org_config(base, {"db_path": str(pinned)})

    env = _local_env(tmp_path / "home", REFLEXIO_LOG_DIR=str(base))
    assert _resolve_db_path(env) == str(pinned)


def test_resolver_defaults_to_legacy_db_when_nothing_exists(tmp_path: Path) -> None:
    _write_org_config(tmp_path, {"db_path": None})

    assert _resolve_db_path(_local_env(tmp_path)) == str(
        tmp_path / ".reflexio" / "data" / "reflexio.db"
    )


def test_resolver_uses_per_org_db_when_only_it_exists(tmp_path: Path) -> None:
    data = tmp_path / ".reflexio" / "data"
    data.mkdir(parents=True)
    (data / "reflexio_claude-smart.db").touch()

    assert _resolve_db_path(_local_env(tmp_path)) == str(
        data / "reflexio_claude-smart.db"
    )


def test_resolver_prefers_legacy_db_when_both_exist(tmp_path: Path) -> None:
    data = tmp_path / ".reflexio" / "data"
    data.mkdir(parents=True)
    (data / "reflexio_claude-smart.db").touch()
    (data / "reflexio.db").touch()

    assert _resolve_db_path(_local_env(tmp_path)) == str(data / "reflexio.db")


def test_resolver_honors_local_storage_path(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    (root / "reflexio_claude-smart.db").touch()

    env = _local_env(tmp_path, LOCAL_STORAGE_PATH=str(root))
    assert _resolve_db_path(env) == str(root / "reflexio_claude-smart.db")


def test_resolver_returns_none_for_non_sqlite_storage(tmp_path: Path) -> None:
    _write_org_config(tmp_path, {"url": "https://example.supabase.co", "key": "x"})

    assert _resolve_db_path(_local_env(tmp_path)) is None


def test_reader_without_sqlite_storage_is_unavailable() -> None:
    script = f"""
import {{ getLearningModelProvenance }} from {json.dumps(READER.as_uri())};
process.stdout.write(JSON.stringify(
  getLearningModelProvenance("profile", "pref-1", null),
));
"""
    result = _run_node(script)
    assert result["unavailable"] is True
    assert result["reason"] == "local storage is not SQLite"


def test_reader_unrelated_tables_without_lineage_is_unavailable(
    tmp_path: Path,
) -> None:
    db = tmp_path / "reflexio.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE profiles (profile_id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO profiles VALUES ('pref-1')")
    conn.commit()
    conn.close()

    result = _run_reader(db, "profile", "pref-1")
    assert result["unavailable"] is True
    assert result["reason"] == "lineage_event table not present"


def test_provenance_is_requested_only_for_local_source() -> None:
    script = f"""
import {{ modelProvenanceAvailable, provenanceRequestKey }}
  from {json.dumps(SOURCE_GUARD.as_uri())};
process.stdout.write(JSON.stringify({{
  localAvailable: modelProvenanceAvailable("local"),
  managedAvailable: modelProvenanceAvailable("managed"),
  unknownAvailable: modelProvenanceAvailable(null),
  local: provenanceRequestKey("local", "profile", "pref-1"),
  managed: provenanceRequestKey("managed", "profile", "pref-1"),
  unknown: provenanceRequestKey(null, "profile", "pref-1"),
  missingId: provenanceRequestKey("local", "profile", ""),
}}));
"""
    result = _run_node(script)
    assert result == {
        "localAvailable": True,
        "managedAvailable": False,
        "unknownAvailable": False,
        "local": "profile:pref-1",
        "managed": None,
        "unknown": None,
        "missingId": None,
    }
