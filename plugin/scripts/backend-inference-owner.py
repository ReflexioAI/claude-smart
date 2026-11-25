#!/usr/bin/env python3
"""Report the selected Reflexio runtime's inference process ownership."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import redirect_stdout
from importlib import import_module
from importlib.util import find_spec
import math
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


def inference_is_colocated() -> bool:
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
            if find_spec("reflexio.removed_env") is not None:
                from reflexio.removed_env import REMOVED_ENV_ALIASES

                # Match the backend launcher. A removed inherited selector
                # must not affect topology; the SDK still checks dotenv keys.
                selector = "REFLEXIO_DEFAULT_ORG_ID"
                if selector in REMOVED_ENV_ALIASES:
                    os.environ.pop(selector, None)
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

        return should_start_local_embedding_service()


def colocated_inference_ready_timeout_seconds() -> int:
    """Return how long the SDK supervisor waits for colocated inference.

    The supervisor bounds that wait itself and exits when it expires, so the
    hook must not treat a launch inside it as stuck. Attached inference is
    not awaited before the API starts, so it adds no time.
    """
    if not inference_is_colocated():
        return 0
    with redirect_stdout(sys.stderr):
        from reflexio.cli import utils
    # Read the deadline the selected SDK's launcher actually uses rather than
    # copying its value; an SDK without it fails the query, never guesses.
    timeout = float(utils._LOCAL_INFERENCE_READY_TIMEOUT_SECS)
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError(f"unusable local inference readiness timeout: {timeout!r}")
    return math.ceil(timeout)


def main() -> int:
    if sys.argv[1:] == ["--validate-windows-embedding"]:
        # The isolated interpreter must use the same vendor source as the
        # eventual backend. The caller sets cwd to the private staging folder.
        vendor = Path(__file__).resolve().parents[1] / "vendor/reflexio"
        if (vendor / "reflexio").is_dir():
            sys.path.insert(0, str(vendor))
        if inference_is_colocated():
            try:
                import_module("onnxruntime")
            except Exception:
                return 77
        return 0
    if sys.argv[1:] == ["--inference-ready-timeout"]:
        print(colocated_inference_ready_timeout_seconds())
        return 0
    print("colocated" if inference_is_colocated() else "attached")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
