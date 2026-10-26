#!/usr/bin/env python3
"""Report the selected Reflexio runtime's inference process ownership."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import redirect_stdout
import os
from pathlib import Path
import sys


def reject_retired_embedding_setting(env: Mapping[str, object] = os.environ) -> None:
    # Source checkouts may use the older PyPI baseline, without removed_env.
    # This read-only probe must reject obsolete policy even with no env file.
    name = "CLAUDE_SMART_USE_LOCAL_EMBEDDING"
    if name in env:
        raise ValueError(
            f"Removed environment name: {name}; preserve the effective model, "
            "endpoint and timeout with REFLEXIO_EMBEDDING_PROVIDER and remove "
            "the old assignment. See the embedding upgrade guide."
        )


def main() -> None:
    reject_retired_embedding_setting()
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
            dotenv_disabled = os.environ.get("PYTHON_DOTENV_DISABLED", "").lower() in {
                "1",
                "true",
                "t",
                "yes",
                "y",
            }
            loaded = load_reflexio_env()
            if loaded is not None and not dotenv_disabled:
                from dotenv import dotenv_values

                # Old SDKs prune blank values; check keys in the file the SDK
                # actually selected, without duplicating its discovery rules.
                reject_retired_embedding_setting(
                    dotenv_values(loaded, interpolate=False)
                )
        reject_retired_embedding_setting()
        from reflexio.cli.run_services import should_start_local_embedding_service

        colocated = should_start_local_embedding_service()
    print("colocated" if colocated else "attached")


if __name__ == "__main__":
    main()
