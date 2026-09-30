#!/usr/bin/env python3
"""Run Reflexio CLI with claude-smart metadata visible in process argv."""

from __future__ import annotations

import json
import os
import runpy
import sys
from pathlib import Path

# The one file every claude-smart release up to 0.2.x stored memory in.
LEGACY_DB_FILENAME = "reflexio.db"


def pin_legacy_sqlite_db_path() -> None:
    """Point the org's SQLite storage at ``reflexio.db`` when no path is set.

    Newer Reflexio resolves a null ``storage_config.db_path`` per org and
    refuses to adopt a shared ``reflexio.db`` holding rows another org
    labelled, opening an empty ``reflexio_<org>.db`` instead -- which made
    0.3.0 installs look wiped. An explicit path bypasses that resolver, so
    this writes the file claude-smart has always used. It must land on disk:
    uvicorn runs in a child process that re-reads the config.

    A path someone already set, and any non-SQLite storage, is left alone.
    Failure is logged and swallowed so a pin problem never stops the backend.

    Returns:
        None: Side effect only -- may rewrite the org config file.
    """
    org_id = os.environ.get("REFLEXIO_DEFAULT_ORG_ID", "").strip()
    if os.environ.get("CLAUDE_SMART_BACKEND") != "1" or not org_id:
        return
    try:
        from reflexio.models.config_schema import StorageConfigSQLite
        from reflexio.server import LOCAL_STORAGE_PATH
        from reflexio.server.services.configurator.local_file_config_storage import (
            LocalFileConfigStorage,
        )

        storage = LocalFileConfigStorage(org_id=org_id)
        config_file = Path(storage.config_file)
        if config_file.exists():
            # load_config swallows a parse error and returns defaults, and
            # saving those would overwrite the user's config. Refuse instead.
            json.loads(config_file.read_text(encoding="utf-8"))
        config = storage.load_config()
        storage_config = config.storage_config
        if not isinstance(storage_config, StorageConfigSQLite):
            return
        if storage_config.db_path is not None:
            return
        storage_config.db_path = str(Path(LOCAL_STORAGE_PATH) / LEGACY_DB_FILENAME)
        storage.save_config(config)
    except Exception as exc:  # noqa: BLE001 - never block backend start
        print(
            f"[claude-smart] could not pin SQLite db_path ({exc!r}); "
            "Reflexio will resolve the database path itself",
            file=sys.stderr,
        )


def main() -> int:
    """Mirror claude-smart metadata into env and delegate to Reflexio CLI.

    Args:
        None: Reads process argv. Arguments before ``--`` are
            ``--claude-smart-*`` metadata kept visible in the process command
            line; arguments after ``--`` become ``reflexio.cli`` arguments.

    Returns:
        int: Exit status. Returns ``2`` when required argument separators or
            Reflexio CLI arguments are missing.
    """
    try:
        separator = sys.argv.index("--")
    except ValueError:
        return 2

    metadata_args = sys.argv[1:separator]
    reflexio_args = sys.argv[separator + 1 :]
    if not reflexio_args:
        return 2

    for arg in metadata_args:
        if not arg.startswith("--claude-smart-") or "=" not in arg:
            continue
        key, value = arg[2:].split("=", 1)
        env_key = key.replace("-", "_").upper()
        os.environ.setdefault(env_key, value)

    pin_legacy_sqlite_db_path()
    sys.argv = ["reflexio.cli", *reflexio_args]
    runpy.run_module("reflexio.cli", run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
