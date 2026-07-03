from __future__ import annotations


def test_backend_compat_adds_all_user_ids_from_interactions(tmp_path):
    from reflexio.server.services.storage.sqlite_storage import SQLiteStorage  # type: ignore[import-not-found]

    from claude_smart.reflexio_backend import _install_sqlite_storage_compat

    if hasattr(SQLiteStorage, "get_all_user_ids"):
        delattr(SQLiteStorage, "get_all_user_ids")

    _install_sqlite_storage_compat()

    storage = SQLiteStorage(org_id="claude-smart-test", db_path=str(tmp_path / "db.sqlite"))
    with storage._lock:
        storage.conn.execute(
            """
            INSERT INTO interactions (user_id, content, request_id, created_at)
            VALUES (?, ?, ?, ?), (?, ?, ?, ?), (?, ?, ?, ?)
            """,
            (
                "project-a",
                "first",
                "req-1",
                "2026-01-01T00:00:01Z",
                "project-b",
                "second",
                "req-2",
                "2026-01-01T00:00:02Z",
                "project-a",
                "third",
                "req-3",
                "2026-01-01T00:00:03Z",
            ),
        )
        storage.conn.commit()

    assert storage.get_all_user_ids() == ["project-a", "project-b"]
