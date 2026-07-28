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
NODE = shutil.which("node")


def _require_node() -> str:
    if not NODE:
        raise AssertionError("node is required to exercise model-lineage.ts")
    return NODE


def _run_reader(db_path: Path | None, entity_type: str, entity_id: str) -> dict:
    node = _require_node()
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
    proc = subprocess.run(
        [node, "--input-type=module", "--eval", script],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={**os.environ, "NODE_NO_WARNINGS": "1"},
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    # node may still emit experimental warnings on stderr; stdout must be pure JSON
    return json.loads(proc.stdout)


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
