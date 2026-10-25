#!/usr/bin/env python3
"""Report the selected Reflexio runtime's inference process ownership."""

from __future__ import annotations

from contextlib import redirect_stdout
import os
from pathlib import Path
import sys


def main() -> None:
    # Older supported SDKs expose this same launcher decision through run_services;
    # current SDKs delegate it to the dependency-free inference launch plan.
    # Keep library output off the small stdout protocol consumed by the shell.
    with redirect_stdout(sys.stderr):
        # The older SDK imports dependencies from its package initializer,
        # before env_loader can install its walkup guard.
        previous = os.environ.get("PYTHON_DOTENV_DISABLED")
        os.environ["PYTHON_DOTENV_DISABLED"] = "1"
        try:
            from reflexio.cli.env_loader import (
                block_implicit_dotenv_walkup,
                load_reflexio_env,
                get_env_path,
            )

            block_implicit_dotenv_walkup()
        finally:
            if previous is None:
                os.environ.pop("PYTHON_DOTENV_DISABLED", None)
            else:
                os.environ["PYTHON_DOTENV_DISABLED"] = previous
        # Old SDKs auto-create a default env on first load. An ownership query
        # must remain read-only; the actual launch/installer owns first-run setup.
        if Path(".env").is_file() or get_env_path().is_file():
            load_reflexio_env()
        from reflexio.cli.run_services import should_start_local_embedding_service

        colocated = should_start_local_embedding_service()
    print("colocated" if colocated else "attached")


if __name__ == "__main__":
    main()
