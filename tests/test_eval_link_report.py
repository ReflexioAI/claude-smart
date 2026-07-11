"""End-to-end tests for the evidence-link health report CLI."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORT = REPO_ROOT / "scripts" / "eval_link_report.py"


def _run_report(
    db: Path, archive: Path, sessions: Path, *, json_output: bool = True
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(REPORT),
        "--db",
        str(db),
        "--archive-dir",
        str(archive),
        "--sessions-dir",
        str(sessions),
    ]
    if json_output:
        command.append("--json")
    return subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def test_report_joins_live_archive_and_session_evidence(tmp_path: Path) -> None:
    db = tmp_path / "reflexio.db"
    archive = tmp_path / "archive"
    sessions = tmp_path / "sessions"
    archive.mkdir()
    sessions.mkdir()
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE profiles (profile_id TEXT, created_at INTEGER);
            CREATE TABLE user_playbooks (user_playbook_id INTEGER, created_at INTEGER);
            CREATE TABLE agent_playbooks (agent_playbook_id INTEGER, created_at INTEGER);
            CREATE TABLE interactions (
                interaction_id TEXT, created_at INTEGER, citations TEXT,
                retrieved_learnings TEXT
            );
            CREATE TABLE requests (
                request_id TEXT, created_at INTEGER, session_id TEXT,
                evaluation_only INTEGER
            );
            INSERT INTO profiles VALUES ('profile-live', 20);
            INSERT INTO user_playbooks VALUES (7, 30);
            INSERT INTO agent_playbooks VALUES (9, 40);
            INSERT INTO interactions VALUES ('i1', 50, '[{"kind":"profile"}]', '[]');
            INSERT INTO interactions VALUES ('i2', 60, '[]', '[{"kind":"profile"}]');
            INSERT INTO requests VALUES ('r1', 50, 'local-and-db', 1);
            INSERT INTO requests VALUES ('r2', 60, 'db-only', 0);
            """
        )
    (archive / "profiles.jsonl").write_text(
        json.dumps(
            {
                "table": "profiles",
                "archived_at": 100,
                "row": {"profile_id": "profile-old", "created_at": 10},
            }
        )
        + "\n"
    )
    (archive / "requests.jsonl").write_text(
        json.dumps(
            {
                "table": "requests",
                "archived_at": 100,
                "row": {
                    "request_id": "r0",
                    "created_at": 10,
                    "session_id": "archive-only",
                    "evaluation_only": 1,
                },
            }
        )
        + "\n"
    )
    (sessions / "local-and-db.jsonl").write_text("{}\n")
    (sessions / "local-only.jsonl").write_text("{}\n")
    registry = [
        {"kind": "profile", "real_id": "profile-live"},
        {"kind": "profile", "real_id": "profile-old"},
        {"kind": "playbook", "source_kind": "user_playbook", "real_id": "7"},
        {"kind": "playbook", "source_kind": "agent_playbook", "real_id": "missing"},
        {"kind": "playbook", "source_kind": "user_playbook"},
    ]
    (sessions / "local-and-db.injected.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in registry)
    )

    result = _run_report(db, archive, sessions)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["retention_targets"]["profiles"] == {
        "live_rows": 1,
        "archived_rows": 1,
        "min_created_at": 10,
        "max_created_at": 20,
    }
    assert payload["interactions"] == {
        "total": 2,
        "citations_non_empty_percent": 50.0,
        "retrieved_learnings_non_empty_percent": 50.0,
    }
    assert payload["registry_links"] == {
        "total_entries": 5,
        "with_real_id": 4,
        "with_real_id_percent": 80.0,
        "resolved": 2,
        "resolved_percent": 50.0,
        "dangling": 2,
    }
    assert payload["session_join"] == {
        "request_sessions": 3,
        "local_sessions": 2,
        "requests_with_local_percent": 33.33,
        "local_with_requests_percent": 50.0,
    }
    assert payload["evaluation_only_requests"] == 2


def test_report_handles_old_interactions_schema_and_never_gates(tmp_path: Path) -> None:
    db = tmp_path / "reflexio.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE interactions (interaction_id TEXT, created_at INTEGER, citations TEXT)"
        )

    result = _run_report(
        db, tmp_path / "missing-archive", tmp_path / "missing-sessions"
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert (
        payload["interactions"]["retrieved_learnings_non_empty_percent"]
        == "column missing"
    )

    human = _run_report(
        db,
        tmp_path / "missing-archive",
        tmp_path / "missing-sessions",
        json_output=False,
    )
    assert human.returncode == 0
    assert "Evidence window" in human.stdout
    assert "retrieved_learnings non-empty: column missing" in human.stdout
