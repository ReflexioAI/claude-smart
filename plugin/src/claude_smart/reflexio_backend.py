"""claude-smart Reflexio backend launcher with local compatibility patches.

The backend is still Reflexio's FastAPI server; this module only installs
claude-smart-scoped shims before handing control to ``reflexio.cli``.
"""

from __future__ import annotations

from typing import Any


def _install_sqlite_storage_compat() -> None:
    """Add storage methods expected by Reflexio services but absent in PyPI builds.

    Reflexio 0.2.25-0.2.27 profile manual generation calls
    ``SQLiteStorage.get_all_user_ids()`` when the API request omits ``user_id``.
    The SQLite backend does not implement that method, so users with collected
    interactions see an AttributeError and get zero generated profiles. The
    method should enumerate users that have interactions, because manual profile
    generation extracts profiles from interaction history.
    """

    try:
        from reflexio.server.services.storage.sqlite_storage import SQLiteStorage  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - backend startup should surface Reflexio errors later.
        return

    if hasattr(SQLiteStorage, "get_all_user_ids"):
        return

    def get_all_user_ids(self: Any) -> list[str]:
        rows = self._fetchall(
            """
            SELECT user_id
            FROM interactions
            GROUP BY user_id
            ORDER BY MIN(created_at), MIN(interaction_id)
            """
        )
        return [row["user_id"] for row in rows]

    setattr(SQLiteStorage, "get_all_user_ids", get_all_user_ids)


def main() -> None:
    """Install claude-smart compatibility patches, then run Reflexio's CLI."""

    _install_sqlite_storage_compat()

    from reflexio.cli.__main__ import main as reflexio_main  # type: ignore[import-not-found]

    reflexio_main()


if __name__ == "__main__":
    main()
